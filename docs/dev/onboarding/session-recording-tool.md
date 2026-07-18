---
id: session-recording-tool
status: experimental
related_issue: "#245"
related_issues:
  - "#136"
  - "#241"
  - "#242"
  - "#243"
created: "2026-05-24"
---

# AI Agent Session 記録ツール Pilot 導入手順書 (SSOT)

本文書は、AI Agent session 記録ツールを人間が pilot 導入する際の**安全な手順書**である。
導入作業のうち権限・secret・外部契約が必要な部分を明確に分離し、
AI が実行できる準備作業と人間が判断しなければならない操作を区別する。

## Codex CLI Hook Trust 補足

- Codex repo-local hook の canonical config key は `[features].hooks`。旧 `codex_hooks` alias が残っていても、それを唯一の正本として説明しない。
- `.codex/hooks.json` を validator が pass しても、runtime active hook state と trust state は別途 `/hooks` 等で確認する必要がある。
- trusted project でない `.codex/` layer の hook は load されない。pilot で `--dangerously-bypass-hook-trust` を既定運用に組み込まない。
- Codex session-recording manifest は private/local artifact に閉じ、public comment や public checkpoint branch に転載しない。出力先は Issue #1546 で repository tree 内の `tmp/session-manifests/codex/**` から、canonical な per-user state root（`XDG_STATE_HOME`、既定 `$HOME/.local/state` 配下の `loop-protocol/session-manifests/v1/<repo_key>/codex/**`）へ移行した。レガシー repo-local root には新規 write されないが、既存 artifact の自動削除は行わない（手動 cleanup 対象）。
- hook は diagnostic / prevention layer であり security boundary ではない。public-safe 判定の正本は post-run validator と posting guard に置く。

## Current public surface boundary

- current live public posting では `agent_session_manifest/v1` の manifest 本文を Issue / PR comment に出さない。公開コメントでは `artifact_digest`、`artifact_url`、`schema_ref`、`validation_verdict` などの opaque ref のみ許可する。
- `agent_run_report/v1` / `agent_retro_index/v1` は #935 schema/redaction validator と #937 exact marker upsert guard が merge されるまで dry-run 専用とし、conditional public comment としての live public posting は禁止する。#936 の lifecycle scripts もこの依存列に含まれる。
- public comment posting は trusted context 限定とし、`pull_request_target` では実行しない。workflow は top-level `contents: read` を baseline にし、posting job に限って `issues: write` / `pull-requests: write` の最小権限を付与する。
- `private artifact` は secret-safe を意味しない legacy term であり、retention-limited non-comment surface を指す。public repo では artifact content も public-safe でなければならない。
- `artifact_url` は retention-limited / auth-dependent / non-canonical locator であり、永続的な証跡 ID として扱わない。canonical identity は `artifact_digest`、`schema_ref`、`validation_verdict`、workflow run URL / comment marker に寄せる。
- 旧 pilot の manifest comment wording や template は historical / non-current / not live public posting の文脈としてのみ読む。current live public posting 許可の根拠には使わない。

---

## Pilot 開始可否

本書の merge は **「導入手順書の整備完了」を意味するに留まる**。
本書が main に merge された時点で pilot を開始してよいわけではない。

実際の pilot 開始は、以下が **すべて main に merge** されるまで禁止する。

- [ ] **Deterministic producer 実装**
      （`.claude/hooks/post-commit-manifest` / `scripts/generate-session-manifest.mjs` /
      `.github/workflows/session-manifest.yml` のいずれか）
- [ ] **Schema validation の実行導線**
      （`docs/schemas/agent-session-manifest.schema.json` を用いた検証スクリプトまたは test）
- [ ] **no-push / private checkpoint / local-only の確認導線**
      （CLI / hook 側の設定が「sessions 自動 push を無効」「checkpoint を local に閉じる」を強制できること）
- [ ] **Kill Switch checker の実行導線**
      （`scripts/check-kill-switch.sh` 相当、または `docs/dev/session-recording-policy.md` の手順を
      機械的に検証する手段）

**禁止事項**: 上記が未実装の状態で、AI self-report manifest を代替として pilot を開始してはならない。
hook/script/CI のいずれも未実装である状態では、metadata auto-recording pilot は開始不可（hook 未実装 → pilot 開始不可）とする。
public repo / public GitHub comment に raw transcript / local_file path / secret-bearing artifact を出すことは禁止（local_file は禁止サーフェスに含む）。
raw transcript を public surface に露出させる経路を有効化することは MUST NOT。
AI 自己申告は authoritative evidence ではなく、本書 §7 で `non_authoritative_sources` に分類されている。

---

## 機械可読メタデータ

```yaml
session_recording_pilot_onboarding:
  schema: v1
  target_audience: human_operators
  context_depends_on:
    - docs/dev/secret-policy.md
    - docs/dev/session-recording-policy.md
    - docs/schemas/agent-session-manifest.schema.json
  pilot_prerequisites:
    - issue_243_manifest_schema_merged: required
    - issue_241_secret_policy_merged: required
    - issue_242_kill_switch_policy_merged: required
  manual_recording_definition: "人間が公開・採用・push を最終判断すること（AI 自己申告を正本にしない）"
  produced_at: "2026-05-24"

pilot_start_gate:
  pilot_start_allowed: false
  required_before_pilot:
    - deterministic_manifest_producer_merged
    - schema_validation_execution_path_merged
    - no_push_or_private_checkpoint_verifier_merged
    - kill_switch_checker_execution_path_merged
  known_followups:
    - "#324"
    - "#325"
    - "#326"
  prohibited_substitute:
    - ai_suggested_manifest
    - ai_self_reported_token_count
```

---

## 1. 候補ツール比較表

### 1.1 詳細比較（Hook-based vs EntireCLI）

| 項目 | EntireCLI | Hook-based Ledger (自前) | 判定 |
|------|-----------|---------------------------|------|
| **記録対象** | full transcript（stdout/stderr 全文） | metadata only（命令・結果・エラーのみ） | EntireCLI: 詳細 / Hook: 軽量 |
| **Secret 露出面** | 高（transcript に含まれうる）| 低（metadata schema で制限） | Hook が secure |
| **Kill Switch 容易さ** | 中（プロセス停止・hook 削除） | 高（hook 無効化のみ） | Hook が容易 |
| **GitHub 親和性** | 中（checkpoint branch push ）| 高（既存 CI/CD と統合可） | Hook が native |
| **学習コスト** | 高（外部ツール習得） | 低（in-repo スクリプト） | Hook が低 |
| **Adopt/Conditional/Reject** | **Conditional** | **Adopt (推奨)** | Hook-based 採用推奨 |

**Conditional の理由**: EntireCLI は「sessions 自動 push 無効化」「private checkpoint」「manual review」が
**必須前提** でのみ候補。default 採用ではない。
（実際に有効化する flag / 設定キー名は本書 §3 の概念例ではなく公式 docs で確認すること）

**Adopt の理由**: Hook-based metadata 記録は既存 `.claude/hooks/` 設計と統合でき、
deterministic producer が保証される。

### 1.2 全候補一覧（Adopt / Conditional / Reject / Defer の 4 分類）

EntireCLI と Hook-based の 2 候補だけでなく、棄却・先送り判断も明示する。
これにより「なぜ AI self-report manifest を採用しなかったか」「なぜ application trace 系を初期 pilot に
含めなかったか」が後続レビューでも参照できる。

| 候補 | 判定 | 主な理由 |
|------|------|---------|
| **Hook-based metadata ledger** | **Adopt** | metadata-only / Secret 露出面が小 / schema validation 可 / 既存 `.claude/hooks/` と統合可 |
| **EntireCLI (local / private / no-push 構成)** | **Conditional** | 疑義発生時の詳細調査用に保留。「auto push 無効」「private checkpoint」「manual review」が揃った場合に限り採用可 |
| **EntireCLI (public checkpoint 構成)** | **Reject** | public branch / transcript exposure リスクが高く、`secrets_mode: none` 前提を壊しうる |
| **AI self-report manifest** | **Reject** | 確率論的自己申告であり authoritative evidence にならない（本書 §7 `non_authoritative_sources` 参照） |
| **LangSmith / OpenTelemetry GenAI 等の application trace 系** | **Defer** | application trace 寄りで初期 pilot には重い。Hook-based で必要 metadata が満たせない場合に再評価する |

`Defer` は「現時点では採用しないが将来再評価する」の意味であり、`Reject` とは区別する。

---

## 2. Pilot 前提条件チェックリスト

以下の条件を **すべて** 満たしてから pilot 導入を開始する。

### 基盤整備の確認

- [ ] **Issue #243** (agent-session-manifest.schema.json) が main に merged 済みか確認
  ```bash
  gh issue view 243 --json state --jq '.state'
  # 出力: CLOSED であること

  # main 上に schema ファイルが存在することを確認する（remote 上の blob 存在チェック）
  git fetch origin main
  git cat-file -e origin/main:docs/schemas/agent-session-manifest.schema.json \
    && echo "PASS: schema file exists on origin/main" \
    || { echo "FAIL: docs/schemas/agent-session-manifest.schema.json not found on origin/main"; exit 1; }
  ```

- [ ] **Issue #241** (secret-policy) が main に merged 済みか確認
  ```bash
  gh issue view 241 --json state --jq '.state'
  # 出力: CLOSED であること
  test -f docs/dev/secret-policy.md && rg "current_secrets_mode: none" docs/dev/secret-policy.md
  ```

- [ ] **Issue #242** (Kill Switch policy) が main に merged 済みか確認
  ```bash
  gh issue view 242 --json state --jq '.state'
  # 出力: CLOSED であること
  test -f docs/dev/session-recording-policy.md
  ```

### 記録 Producer の確認

- [ ] Deterministic producer が存在するか確認（hook / script / CI のいずれかが必須）
  ```bash
  # 4 種のうち最低 1 つが存在すれば PASS、すべて欠落していれば pilot 開始禁止
  if test -f .claude/hooks/post-commit-manifest \
    || test -f .claude/hooks/pre-push-manifest \
    || test -f scripts/generate-session-manifest.mjs \
    || test -f .github/workflows/session-manifest.yml; then
    echo "PASS: deterministic producer found"
  else
    echo "FAIL: deterministic producer missing; pilot start prohibited" >&2
    exit 1
  fi
  ```
  - **If hook/script/CI が未実装**: AI self-report manifest を代替にしてはならない（後述「手順5」参照）

- [ ] Schema validation コマンドが存在するか確認
  ```bash
  # schema ファイルが存在するか
  test -f docs/schemas/agent-session-manifest.schema.json

  # validation テストが存在するか（推奨: in-repo test を一次根拠とする）
  test -f tests/agent-session-manifest.test.ts

  # 実行で PASS を確認する（例）
  pnpm test -- --run tests/agent-session-manifest.test.ts

  # 補助: jq などの CLI で構文チェックする場合は `command -v` を使う
  #   （`test -c "which jq"` は char device 判定であり、ツール存在チェックとして意味を成さない）
  command -v jq >/dev/null 2>&1 && echo "jq available" || echo "jq missing"
  ```

- [ ] Checkpoint branch 検証コマンドが成功するか確認（fail-on-found）
  ```bash
  # 公開 checkpoint branch が誤って origin に存在していたら FAIL とする
  test -z "$(git ls-remote --heads origin entire/checkpoints/v1)" \
    && echo "PASS: public checkpoint branch not found on origin" \
    || { echo "FAIL: entire/checkpoints/v1 exists on origin; pilot start prohibited"; exit 1; }
  ```

### Secret 状態の確認

- [ ] `secrets_mode: none` であることを確認
  ```bash
  rg "current_secrets_mode: none" docs/dev/secret-policy.md
  # マッチ: 実施可能
  # マッチしない: 秘密管理状態を見直してから導入
  ```

---

## 3. 安全な Pilot 設定

### 3.1 No-Push 制御

public repo への automatic push を **絶対禁止** する。

> **EntireCLI 設定の根拠**
>
> 以下の `entire ...` コマンドと `.entire/settings.local.json` 構造は公式 docs に準拠する。
> 詳細は以下を参照:
>
> - <https://docs.entire.io/cli/configuration> — `strategy_options.push_sessions` / `telemetry` 設定キー
> - <https://docs.entire.io/cli/commands> — `enable` / `configure` / `status` サブコマンド
> - <https://docs.entire.io/cli/checkpoints> — `--checkpoint-remote` flag と checkpoint 配置先
>
> 本書が要求するのは「**push_sessions を false にする / telemetry を無効化する /
> checkpoint remote を public origin に出さない**」というセキュリティ要件である。
> 公式 CLI のバージョン差で flag 名が変わった場合は、当該バージョンの help / docs を必ず確認すること。

#### EntireCLI を使う場合

```bash
# 1. project-local に有効化する（global 設定を汚さない）
entire enable --local --skip-push-sessions --telemetry=false

# 2. 既存設定を後から書き換える場合
entire configure --local --skip-push-sessions --telemetry=false

# 3. 設定状態を確認する
entire status --detailed
```

`.entire/settings.local.json`（project-local。公式 docs に準拠した JSON 構造）には少なくとも以下のキーが含まれていることを確認する。

```json
{
  "strategy_options": {
    "push_sessions": false
  },
  "telemetry": false
}
```

> **#242 との整合**
>
> `docs/dev/session-recording-policy.md` の `auto_push_sessions_allowed: false` は
> 「session の自動 push を禁止する」という方針を policy 側で宣言したもの。
> EntireCLI 側の `strategy_options.push_sessions: false` は、その方針を CLI 設定で
> 機械的に enforce する具体実装に相当する。両者は同じ意図を二重で固定するための
> 多層防御であり、いずれか一方だけが false でも pilot は開始しない。

#### Hook-based 記録を使う場合

```bash
# .git/config または .claude/config.local.json で no-push を設定
# 例: Hook の自動 push ロジックを無効化
export MANIFEST_SKIP_PUSH=true

# Hook 実行時に push を防ぐ
pre-push-manifest --dry-run --no-push
```

### 3.2 Private Checkpoint / Local-Only 設定

記録データを private 環境に限定する。

#### EntireCLI の場合

```bash
# Private checkpoint repository を別途人間が作成しておく
# 例: GitHub 上で myorg/checkpoints-private を private repo として作成

# checkpoint を private remote に向ける（公式 flag）
entire enable --checkpoint-remote github:myorg/checkpoints-private \
              --local --skip-push-sessions --telemetry=false

# 状態確認
entire status --detailed
```

> **#242 `auto_push_sessions_allowed: false` との整合**
>
> `--checkpoint-remote` は「checkpoint」（snapshot/diff layer）の push 先であり、
> 「session 本体」の自動 push とは別レイヤである。
> `auto_push_sessions_allowed: false` は session 本体の auto-push を policy で禁じており、
> `--skip-push-sessions` がその enforce 経路。private checkpoint remote を使う場合でも
> session 本体は push しないという原則は変わらない。
> Private checkpoint remote の利用は **人間が visibility と push 挙動を確認した後にのみ**
> 有効化する。

#### Hook-based の場合

```bash
# Manifest を local artifacts/ に保存（public repo に push しない）
export MANIFEST_OUTPUT_DIR=artifacts/session-manifests

# Verification: artifacts/ は .gitignore 対象であることを確認
test -f .gitignore && grep -q "^artifacts/" .gitignore
# または手動追加が必要な場合は「手順4」参照
```

### 3.3 Manual Review Required

記録データの **公開・採用・push を人間が最終判断** する。

#### Manual Recording の定義

本手順書における「manual recording」とは以下を意味する：

- **「AI が自己申告した manifest は正本ではない」**
- **「AI agent が事前に『recording した』と宣言するのを禁止」**
- **「人間が実際の hook 出力 / CI ログ / validation 結果を確認し、公開を判断する」**

#### AI-Suggested Manifest の禁止ルール

以下のパターンは **禁止** である：

- [ ] AI が自由記述で session manifest を生成し、それを authoritative 証拠として扱う
- [ ] AI が `ai_self_reported_token_count` フィールドで token 使用量を自己報告する
- [ ] AI が hook ログなしで「記録しました」と報告する
- [ ] AI が deterministic evidence（hook ログ / CI 成功ログ）なしで manifest を承認扱いにする

#### Manual Review チェックリスト

```bash
# Hook/CI が manifest を生成したことを確認
ls -la artifacts/session-manifests/ | grep agent-session-manifest-*.json
# または
git log --oneline --grep="session-manifest" | head -5

# Hook ログに PASS が記録されていることを確認
cat artifacts/session-manifest-validation.log
# マッチ: "Validation: PASS"

# Manifest 内容を手で確認（Token count / producer / timestamp 等）
cat artifacts/session-manifests/agent-session-manifest-*.json | jq '.'

# producer フィールドが schema enum に収まっていることを確認
# （hook_generated / script_generated / github_action_generated）
# human_attested_from_deterministic_evidence は schema enum 外の運用 attestation
cat artifacts/session-manifests/agent-session-manifest-*.json | jq '.producer'

# AI-suggested フラグが含まれていないことを確認
# 注意: `grep -v` は「マッチしない行のみ出力」する filter であり、
#       「含まれていないこと」を検証するには使えない（誤検証になる）。
#       「禁止語が 1 つも存在しないこと」は exit code を反転して確認する。
! grep -R '"ai_suggested_manifest"' artifacts/session-manifests/ \
  && echo "OK: ai_suggested_manifest なし" \
  || { echo "FAIL: ai_suggested_manifest を検出"; exit 1; }

! grep -R '"ai_self_reported_token_count"' artifacts/session-manifests/ \
  && echo "OK: ai_self_reported_token_count なし" \
  || { echo "FAIL: ai_self_reported_token_count を検出"; exit 1; }
```

---

## 4. Kill Switch 連動

Session 記録ツールが security incident / secret leak を引き起こした場合の即時停止手順を把握する。

### Kill Switch トリガー条件

以下のいずれかに該当した場合、即座に Kill Switch を実行する：

1. **`secrets_mode` が `none` 以外に遷移した**
2. **checkpoint token が誤って repo に commit された**
3. **public checkpoint branch が誤って push された**
4. **raw transcript が GitHub public comment に出力された**
5. **Secret scan tools が credential to flag した**
6. **public repo への automatic push が有効化された**

### Kill Switch 実行手順

詳細は **[`docs/dev/session-recording-policy.md`](../session-recording-policy.md)** の「Kill Switch 手順」セクションを参照する。

**概要**:

1. **Session 記録ツール即時停止** — プロセス kill / auto-start 無効化
   ```bash
   ps aux | grep -E "(entire|checkpoint|session-record)" | grep -v grep | awk '{print $2}' | xargs kill -9 2>/dev/null || true
   ```

2. **Claude Code / Git hook 無効化** — 公式の `disableAllHooks` を主導線にする
   公式 docs: <https://docs.claude.com/en/docs/claude-code/hooks>

   ```bash
   # 主手順: Claude Code 全 hook を一括停止（settings JSON で宣言的に無効化）
   mkdir -p .claude
   tmp="$(mktemp)"
   jq '.disableAllHooks = true' .claude/settings.local.json 2>/dev/null > "$tmp" \
     || printf '{ "disableAllHooks": true }\n' > "$tmp"
   mv "$tmp" .claude/settings.local.json

   # 個別 hook を残しつつ session 記録系だけ外したい場合は settings JSON の
   # `hooks` エントリから該当 event handler を `jq 'del(...)'` で削除する。
   # 例: SubagentStop hook の session 記録 entry を削除
   #   jq 'del(.hooks.SubagentStop)' .claude/settings.local.json > "$tmp" && mv "$tmp" .claude/settings.local.json
   ```

   ```bash
   # 補助: .git/hooks に session 記録の補助スクリプトが存在する場合の追加措置
   # （settings JSON の無効化で hook event 自体は発火しなくなるが、
   #   .git/hooks/ の shell スクリプトが個別に起動される運用なら実行権限も落とす）
   chmod -x .git/hooks/pre-commit 2>/dev/null || true
   chmod -x .git/hooks/pre-push 2>/dev/null || true
   chmod -x .claude/hooks/* 2>/dev/null || true
   ```

   **SubagentStop hook 実装ガイダンス（無効化の前提として、有効化時の最低要件）**
   - SubagentStop hook で transcript 本文を読み取らない（metadata schema fields のみ参照）
   - 取得した値を public surface（GitHub PR comment / public branch / public artifact）に出さない
   - `.env` / `*.pem` / API key を含むパスは skip する
   - hook 内で扱う path / 環境変数はすべて shell quote する（`"$VAR"` 形式）
   - 受け取った path に `..` / 絶対 path 抜けが含まれる場合は reject する（path traversal 防止）

3. **Push remote visibility 確認** — public repo への push 防止
   ```bash
   git remote -v | grep origin
   gh repo view --json visibility --jq '.visibility'
   # 出力: PRIVATE であること（public なら設定変更が必要）
   ```

4. **Checkpoint branch 削除**
   ```bash
   git branch -r | grep "entire/checkpoints"  # remote branch 確認
   git push origin --delete entire/checkpoints/v1 2>/dev/null || true
   ```

5. **Leaked credentials 対応** — 誤って commit/push された secret は revoke / rotate
   - EntireCLI token が混入: EntireCLI dashboard で token revoke
   - GitHub token が混入: GitHub Settings → Developer settings で Personal Access Token revoke
   - その他 API key: 該当サービスで即座に revoke / rotate

---

## 5. 撤退手順

Pilot が失敗した場合、または本導入に移行できない場合の撤退手順を事前に確認する。

### Step 1: ツール Disable

#### EntireCLI の場合（公式コマンド主導）

公式 CLI で停止・無効化・cleanup・token 失効まで完結できる。`kill -9` 等の手段は最後の手段に
回し、まず tool-native の停止経路を試す。

```bash
# 1. Entire の通常停止（session の正常終了 → project からの disable）
entire session stop --all || true
entire disable --project || true

# 2. 連携 agent hook を外す
entire agent remove claude-code || true
entire agent remove codex || true

# 3. local session data の cleanup（dry-run で対象確認 → 実行）
entire clean --dry-run
entire clean --all --force

# 4. token / 認証情報の失効が必要な場合
entire auth revoke --current || true
```

```bash
# 最後の手段: 上記公式コマンドがすべて失敗し、なお entire 関連プロセスが残留している場合のみ
#   ps + kill を用いる。通常の撤退手順では使用しない。
ps aux | grep entire | grep -v grep | awk '{print $2}' | xargs -r kill -9

# 起動スクリプト / alias を「特定して」削除する
# 注意: home 配下を sed -i で機械置換することは行わない（破壊的）。
#       下記は「検出のみ」。実際の削除は対象を目視確認してから手動で実施する。
grep -RIn "entire session start" ~ 2>/dev/null | grep -v ".history" || true

# .entire/settings.local.json の session 記録設定を見直す場合も、機械置換ではなく
# 該当キーを手動で確認・削除する。
```

#### Hook-based の場合
```bash
# Hook ファイルを無効化（削除ではなく disable を推奨 — 後で復旧可能）
chmod -x .claude/hooks/post-commit-manifest
chmod -x .claude/hooks/pre-push-manifest
chmod -x .git/hooks/pre-commit
chmod -x .git/hooks/pre-push

# または削除（復旧不可）
rm -f .claude/hooks/post-commit-manifest
rm -f .claude/hooks/pre-push-manifest
```

### Step 2: Checkpoint Branch 削除

```bash
# Remote branch を削除
git push origin --delete entire/checkpoints/v1 2>/dev/null || echo "Branch not found"
git push origin --delete checkpoints/metadata 2>/dev/null || echo "Branch not found"

# Local tracking branch を削除
git branch -r | grep "entire\|checkpoints" | xargs -I {} git branch -rd {}

# 確認
git branch -r | grep -i checkpoint
# 出力が空であること
```

### Step 3: Hook / Manifest Script 削除

```bash
# Hook ディレクトリをクリーンアップ
rm -rf .claude/hooks/session-manifest/
rm -f .claude/hooks/post-commit-manifest
rm -f .claude/hooks/pre-push-manifest

# Manifest generation script を削除
rm -f scripts/generate-session-manifest.mjs
rm -f scripts/validate-manifest.sh

# Artifacts 削除
rm -rf artifacts/session-manifests/
rm -rf artifacts/session-manifest-validation.log

# .gitignore から artifacts/ 行を削除（後に復旧予定なら Comment out）
sed -i '/#.*artifacts/!{/^artifacts\/$/d;}' .gitignore || true
```

### Step 4: Remaining Transcript 取り扱い

```bash
# Local artifacts/ に残存したログを確認
ls -la artifacts/

# Secure deletion（一度削除すると復旧不可）
# 機密情報が含まれる場合
#
# 注意:
#   - `find ... -name A -o -name B` は演算子優先順位の都合で意図しないマッチを起こす。
#     必ず `\( ... \)` で OR をグループ化する。
#   - ファイル名に空白・改行が含まれる可能性に備え、`-print0` + `xargs -0` を使う。
find artifacts/ \( -name "*.log" -o -name "*.json" \) -print0 \
  | xargs -0 shred -vfz -n 3 2>/dev/null \
  || find artifacts/ \( -name "*.log" -o -name "*.json" \) -print0 \
       | xargs -0 rm -f

# または off-repo に暗号化保存して security review に回す
#
# 推奨: `tar | gpg --symmetric` 経路に一本化する。
# 禁止: `zip -e -P <password>` は password がシェル履歴・プロセスリストに残るため使用禁止。
#       `zip -e remaining-transcripts.zip artifacts/` を「非対話で実行できる」例として
#       書いてはならない（実際には対話入力を要求するため snippet として誤りやすい）。
#
# 暗号化保存（password は対話入力。`--batch --passphrase` は履歴に残るため避ける）
tar -czf - artifacts/ | gpg --symmetric --cipher-algo AES256 \
  -o remaining-transcripts.tar.gz.gpg

rm -rf artifacts/

# Commit して撤退を記録
git add -A
git commit -m "chore: session recording tool pilot 撤退 — hook 削除・manifest script 削除・artifacts 削除"
```

---

## 6. Responsibility Split（責務分離）

### Human-Only Responsibilities（人間が実行しなければならない作業）

以下の作業は **AI が代替できない** ため、人間が実行する。

| 作業 | 理由 | 手順の所在 |
|------|------|---------|
| **外部サービス契約・利用規約受諾** | EntireCLI / 他ツールの利用契約は人間の法務判断が必須 | ツール提供元の利用規約を参照 |
| **Account 作成・認証 token 発行** | API key / token の security責任 | ツール設定手順を参照 |
| **Private checkpoint repository 作成** | GitHub organization 設定・private 設定は admin 権限必須 | GitHub repo creation UI |
| **secret / token の設定** | `.env` / `.entire/config.yaml` への token 記入 | 各ツールのドキュメント |
| **Public repo への push / 公開採用の最終判断** | Session data の公開判断は人間の責務 | 「手順3」の Manual Review セクション |
| **Kill Switch 実行** | Secret leak / incident 対応は人間が判断・実行 | 「手順4」参照 |
| **Transcript の security review / 削除判定** | Leaked data の取り扱いは人間が判断 | 「手順5」参照 |

### AI-Agent Responsibilities（AI が実行対象に含める作業）

以下の作業は **AI が安全に実行できる準備作業** であり、人間に「やらせず」に AI が主導する。

| 作業 | 実装形式 | 責務 |
|------|---------|------|
| **Hook / Manifest generation script 作成** | `.claude/hooks/post-commit-manifest` / `scripts/generate-session-manifest.mjs` | 決定論的・deterministic ・検証可能 |
| **Hook スケルトン実装** | Git hook template （実行権限は人間が付与） | template 提供・local test 実施 |
| **SubagentStop hook 連動スクリプト** | `.claude/hooks/subagent-stop-manifest` | SubAgent stop event を capture し manifest 生成 |
| **Schema validation script** | `scripts/validate-manifest.sh` / `jq` schema check | manifest の integrity 検証・artifact 出力 |
| **Local-only / no-push テンプレート** | Config template / environment variable export | Hook 内に `--dry-run --no-push` を埋め込み |
| **Kill Switch 確認スクリプト** | `scripts/check-kill-switch.sh` | hook disable / process kill 確認 |
| **Repository-local 安全設定** | `.git/config` / `.gitignore` 追加 | public push 防止・artifacts ignore 設定 |
| **Deterministic manifest producer 実装** | Hook / CI ベース（self-report ではない） | proof of execution（exit code 0）を記録 |

### ルール: AI 実行可能な作業を人間作業に押し付けない

以下の anti-pattern は **禁止** である：

- [ ] 「hook は複雑だから後で人間が書いてください」と提案しない
  - AI が hook skeleton を実装・テストして人間に確認させるのが正規経路
  
- [ ] 「script validation は省いて手動確認で対応してください」と提案しない
  - AI が validation script を実装し、人間が「実行結果を確認」する形に
  
- [ ] 「Manifest template は example として出力するので人間が実装してください」と提案しない
  - AI が deterministic producer を実装し、人間が「承認」する形に

---

## 7. Authoritative Producer 分類

Session manifest が信頼できる出所（authoritative source）であるかどうかを判定するフレームワーク。

### Authoritative Producers（証跡として信頼できる生成者）

```yaml
authoritative_sources:
  - hook_generated:
      definition: "Git hook / SubagentStop hook 等の決定論的スクリプトが生成"
      trustworthiness: high
      evidence: "hook exit code 0 + timestamp + .git/hooks/post-commit log"
      example: ".claude/hooks/post-commit-manifest がmanifest.json を生成"

  - script_generated:
      definition: "CI スクリプト・自動化スクリプトが生成（idempotent / reproducible）"
      trustworthiness: high
      evidence: "script exit code 0 + script source code in repo"
      example: "scripts/generate-session-manifest.mjs が artifacts/session-manifest.json を出力"

  - github_action_generated:
      definition: "GitHub Actions workflow が生成（checksum / log 保存）"
      trustworthiness: high
      evidence: "workflow run log + artifact upload"
      example: ".github/workflows/session-manifest.yml が manifest.json を artifacts に保存"

  - human_attested_from_deterministic_evidence:
      definition: "上記 authoritative source の出力に基づき、人間が証明"
      trustworthiness: high
      evidence: "hook log + human signature / GitHub comment 上の opaque ref / commit message"
      example: "Hook ログと artifact digest を確認して人間が『この manifest ref は信頼できる』と comment"
```

### Non-Authoritative Producers（証跡として信頼できない生成者）

```yaml
non_authoritative_sources:
  - ai_suggested_manifest:
      definition: "AI agent が自己申告・自由記述で生成した manifest"
      trustworthiness: none
      prohibition: "MUST NOT be treated as authoritative evidence"
      example: "AI が『token count: 50000』と自己報告してmanifest に記入"
      consequence: "authoritative source の証拠がないため、実装・monitoring に採用できない"

  - ai_self_reported_token_count:
      definition: "AI が token_usage.counted_by を『ai_self_reported』にして記入"
      trustworthiness: none
      prohibition: "MUST NOT use. Forbidden in token_usage field"
      example: "token_usage.counted_by = 'ai_self_reported'; token_usage.token_count = 50000"
      consequence: "Token 使用量の accurate billing / monitoring が不可"
```

### ルール

以下を **必ず遵守** する：

- [ ] `ai_suggested_manifest` をmanifest schema に含めない
- [ ] AI が `ai_self_reported_token_count` で token count を記入しない
- [ ] `producer.kind` は `script_generated | hook_generated | github_action_generated` のみを許可する
- [ ] `human_attested_from_deterministic_evidence` は schema enum ではなく運用上の attestation として扱う
- [ ] Hook / script / CI ログなしで manifest を「正本」として扱わない
- [ ] Manual review とは「deterministic evidence を人間が確認すること」を意味する（AI 申告を信用する意味ではない）

---

## 8. チェックリスト：導入準備

以下を確認してから pilot 導入を開始する。

```markdown
## Pre-Pilot Checklist

### 基盤確認
- [ ] Issue #243 (manifest schema) merged
- [ ] Issue #241 (secret-policy) merged + `secrets_mode: none` 確認
- [ ] Issue #242 (Kill Switch policy) merged
- [ ] `docs/dev/session-recording-policy.md` が存在

### producer / validation 確認
- [ ] Hook / script / CI のいずれかが deterministic producer として存在する
  - [ ] `.claude/hooks/` に manifestgeneration script がある
  - [ ] または `scripts/generate-session-manifest.mjs` がある
  - [ ] または `.github/workflows/session-manifest.yml` がある
- [ ] Schema validation が存在
  - [ ] `docs/schemas/agent-session-manifest.schema.json` が存在
  - [ ] `tests/agent-session-manifest.test.ts` が存在
  - [ ] `pnpm test -- tests/agent-session-manifest.test.ts` または repository 既定の schema test が PASS
  - [ ] `jq` は JSON 整形・フィールド確認の補助であり、schema validation の代替ではない
- [ ] Checkpoint branch validation コマンドが成功
  - [ ] `git ls-remote origin refs/heads/entire/checkpoints/v1` が空

### Secret 状態確認
- [ ] `docs/dev/secret-policy.md` で `current_secrets_mode: none` を確認
- [ ] `.env` / `.entire/config.yaml` に secret が誤って含まれていないか確認
- [ ] GitHub Actions Secrets に余計な secret が登録されていないか確認

### 安全設定
- [ ] No-push 設定を確認（EntireCLI: 公式 docs で `auto_push_sessions: false` 相当の flag/設定を特定し有効化, Hook: `MANIFEST_SKIP_PUSH=true`）
- [ ] Private checkpoint / local-only 設定を確認
- [ ] `.gitignore` に `artifacts/` / `session-manifests/` が含まれているか確認
  - [ ] ない場合は追加予定

### Manual Review ルール確認
- [ ] 「manual recording」= 人間が公開・採用を判断することを理解
- [ ] AI-suggested manifest を禁止することを理解
- [ ] `ai_self_reported_token_count` を禁止することを理解
- [ ] Deterministic evidence（hook log / CI log）必須を理解

### Kill Switch 理解
- [ ] Kill Switch トリガー条件を認識
- [ ] Kill Switch 手順（`docs/dev/session-recording-policy.md` 参照）を読了
- [ ] 緊急時の連絡先・エスカレーション先を確認

### Withdrawal 準備
- [ ] 撤退手順（本文書「手順5」）を読了
- [ ] Checkpoint branch 削除コマンドを確認
- [ ] Hook 無効化コマンドを確認
- [ ] Secret revoke / rotate 手順を確認

### Final Sign-Off
- [ ] 上記すべてが確認済みか：**YES / NO**
- [ ] 導入を進める権限を持つ人物が確認したか：**YES / NO**
- [ ] 日付：
```

---

## 9. FAQ / トラブルシューティング

### Q: Hook 未実装時は pilot 導入できるか？

**A: No.** Hook / script / CI が未実装の場合、deterministic producer が存在しないため、
AI self-report manifest を代替にすることはできない。

```bash
# Hook が存在するか確認
test -f .claude/hooks/post-commit-manifest || \
test -f scripts/generate-session-manifest.mjs || \
test -f .github/workflows/session-manifest.yml || \
  echo "FAIL: deterministic producer が見つかりません。pilot 開始不可。"
```

後続 Issue を起票して hook / script を実装してから pilot を開始する。

### Q: 「AI-suggested manifest」とは何か？

**A:** AI が Hook ログなしで自由記述で生成した manifest。以下は禁止：

- AI が「記録しました」と自己申告するだけで manifest を生成
- AI が Hook実行ログなしで `token_count` を記入
- AI が `producer: "ai_self_reported"` で記入

正規の manifest は Hook / CI / script のログを基に人間が verify したもの。

### Q: Secret が repo に混入した場合は？

**A:** Kill Switch を実行する（「手順4」参照）：

1. 記録ツール即時停止
2. Git hook 無効化
3. Checkpoint branch 削除
4. 誤 commit / push から secret を除去（`git filter-repo` / history rewrite）
5. 該当 service で secret revoke / rotate
6. `docs/dev/session-recording-policy.md` の「漏洩時手順」を参照

### Q: Public repo では pilot 導入できるか？

**A: Hook-based metadata-only pilot は条件付きで可。EntireCLI full transcript pilot は原則不可。**

候補ごとに採否を分離する。両者を一括で「Yes with controls」と扱うと、full transcript の
public 流出を許容しているように読まれるため、本書では明確に切り分ける。

- **Hook-based metadata-only**（条件付き可）
  - public surface（PR comment / public branch / public artifact）に raw transcript /
    local_file path / secret-bearing artifact を出さない
  - schema validation（`tests/agent-session-manifest.test.ts` 等）と
    Kill Switch checker (`scripts/check-kill-switch.sh` 相当) が PASS
  - `docs/dev/secret-policy.md` の `current_secrets_mode: none` を確認
  - `MANIFEST_SKIP_PUSH=true` を hook 内で enforce

- **EntireCLI**（原則不可。以下をすべて満たす local-only 試用に限る）
  - `.entire/settings.local.json` の `strategy_options.push_sessions: false` が確認できる
  - `entire/checkpoints/v1` を public origin に **push しない**
    （`git ls-remote --heads origin entire/checkpoints/v1` が空であること）
  - private checkpoint remote を使う場合も、その remote の visibility と
    自動 push 挙動を人間が事前確認する
  - EntireCLI 側の transcript redaction は「保証」ではなく safety net として扱い、
    public 流出抑制の一次手段は「そもそも push しない」設定に置く

Public repo での pilot は full transcript 経路でリスクが高い。Private repo での
先行 pilot、または Hook-based metadata-only に絞った pilot を推奨する。

### Q: Pilot の成功基準は何か？

**A:** 以下をすべて満たす：

1. Hook / script / CI がmanifest を deterministic に生成
2. Schema validation が PASS
3. No-push / local-only 設定が有効
4. 人間による manual review が実施
5. Secret が誤って出力されていない
6. Kill Switch 手順が確認できた
7. 撤退手順が確認できた

3ヶ月以上安定稼働した時点で本導入への検討を開始。

---

## 参考リンク

- [Secret Inventory と no-secret 運用境界](../secret-policy.md) — `docs/dev/secret-policy.md`
- [Session 記録 Kill Switch Policy](../session-recording-policy.md) — `docs/dev/session-recording-policy.md`
- [Agent Session Manifest Schema](../../schemas/agent-session-manifest.schema.json) — `docs/schemas/agent-session-manifest.schema.json`
- [Agent / Skill 責務境界](../agent-skill-boundaries.md) — `docs/dev/agent-skill-boundaries.md` の Hook-based Ledger 設計
- [Parent Goal Issue #136](https://github.com/squne121/loop-protocol/issues/136)

---

**Last Updated**: 2026-05-24  
**Status**: experimental (pilot 導入 SSOT)  
**Maintained By**: Implementation team
