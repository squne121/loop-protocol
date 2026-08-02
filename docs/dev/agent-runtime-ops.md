# Agent Runtime Ops

Codex local runtime 運用の主文書。
この文書は Codex CLI の起動・sandbox・permission profile・rules・instruction surface を扱い、GitHub 操作ルールそのものは [github-ops.md](github-ops.md) を参照する。

## Runtime Positioning（実行時の位置付け）

- Codex CLI は **optional runtime** であり、`Claude Code`、既存 `SSOT`、既存 `workflow` を置き換えない
- repo の正本は引き続き `CLAUDE.md`、`docs/dev/workflow.md`、`docs/product/requirements.md` などの SSOT 群にある
- Codex 向けの project-local guidance は `AGENTS.md` に集約し、この文書はその runtime 前提と復旧手順を補足する

## Agent model allocation declaration proof（エージェントモデル割当の宣言証明）

`tests/fixtures/codex-agent-config/expected-runtime-contract.json` が custom agent のモデル、reasoning effort、permission 宣言の唯一の declaration proof である。TOML と静的 validator はこの契約への一致を検査するが、宣言値を provider-side dispatch の観測値として扱わない。

証明は三層に分ける。declaration proof は contract/TOML の静的一致、dispatch proof はイベントが存在する場合に trusted hook が記録する observed model と session/turn/agent/run の相関、availability proof は同一 evidence run の全distinct Terra/Luna model/effort direct smokeである。Issue #1451 のruntime完了条件はavailability proofに限定し、custom-agent dispatchやfresh ledger生成を要求しない。ledger境界はstatic validator/fixtureで維持する。ledger は secret を保存せず、hook trust、freshness、repo headを検証できない場合は PASS にせず `HUMAN_ACTION_REQUIRED` または `BLOCKED` とする。

allocation を戻す必要がある場合は、contract、全対象TOML、hook/evidence validator、fixture を同一コミットで原子的に戻す。片面だけのrollbackや、過去runのledger再利用は許可しない。

## Network-Only Auto Allow（ネットワーク限定の自動許可）

この節は **network boundary の差分例** であり、permission profile 全体を列挙する complete profile ではない。
`uploads.github.com` は GitHub の release asset / upload 系 endpoint 向けで、issue / PR の投稿やコメントの主経路ではない。
GitHub issue / PR の更新・コメントは引き続き [github-ops.md](github-ops.md) と `rtk gh` を使う。

### Modern example（現行設定例）

```toml
approval_policy = "on-request"
default_permissions = "loop-protocol-personal-dev"

[permissions.loop-protocol-personal-dev]
extends = ":workspace"

[permissions.loop-protocol-personal-dev.network]
enabled = true
mode = "full"
allow_local_binding = false

[permissions.loop-protocol-personal-dev.network.domains]
"**.github.com" = "allow"

[permissions.loop-protocol-rtk.network]
enabled = true

[permissions.loop-protocol-rtk.network.domains]
"github.com" = "allow"
"api.github.com" = "allow"
"objects.githubusercontent.com" = "allow"
"uploads.github.com" = "allow"
```

- root scope の `default_permissions` は repository-defined custom profile `loop-protocol-personal-dev` である。`extends = ":workspace"`（OpenAI 公式 built-in profile。filesystem read は OS 権限が許す範囲で制限しない、write は active workspace roots と system temporary directories に限定し `.git`/`.agents`/`.codex` は protected metadata として書き込めない）した上で、個人趣味開発向けの broad な development network allowlist（`network.mode = "full"`、`allow_local_binding = false`）を明示的に layer している（owner の `HUMAN_PERMISSION_DECISION_V1`、Issue #1859）。この節の `loop-protocol-rtk` の network allowlist 差分は別の **repository-defined custom profile** に属し、agent-local にのみ選択される
- `default_permissions` と `[permissions.*]` だけを使い、legacy `sandbox_workspace_write` には依存しない
- `uploads.github.com` は release asset / upload 系の経路に限定して `loop-protocol-rtk` へ追加している
- `loop-protocol-readonly` と `loop-protocol-bootstrap` には追加しない。どちらも upload / release asset の許可を必要としないため、read-only / bootstrap の境界を狭く保つ
- `loop-protocol-rtk` を root default にしない。root default である `loop-protocol-personal-dev` の network allowlist は明示的な development domain のみで、`"*"` のような global allowlist や `allow_local_binding = true` は含まない

### Legacy compatibility note（旧設定との互換注記）

```toml
# legacy runtime only
[sandbox_workspace_write]
network_access = true
```

- `network_access = true` は legacy runtime のみの表現で、modern `default_permissions` と混在させない
- GitHub issue / PR updates and comments still use [github-ops.md](github-ops.md) and `rtk gh`

## Root Default Permission Profile（root default 権限プロファイル、Issue #1859）

### 背景

PR #1849（P0 quarantine）は root scope の `default_permissions` を削除した。Codex CLI 0.146.0 の
loader は非空の `[permissions.*]` を持つ設定で root default が欠落していると
`config defines [permissions] profiles but does not set default_permissions` を返して起動を拒否し、
`codex status` / App Server の `skills/list` reload / 対話 TUI の Skill refresh が失敗する。先行 PR
#1864（本 Issue 初回実装）は root default を built-in `:workspace` として復元したが、敵対的レビュー
で 4 blocker（`:workspace` の filesystem read 境界の誤説明、isolated-home smoke の project trust
不在、AC2/AC4 の runtime verification 未実施での follow-up 延期、JS validator の生テキスト regex に
よる root scope 誤検知見逃し）が指摘された。owner はこれを受け、root default を `:workspace` を
継承しネットワークを有効化した repository-defined custom profile `loop-protocol-personal-dev` へ変更
する再改訂を `HUMAN_PERMISSION_DECISION_V1` として確定した（personal hobby development 向けの
risk posture）。

### 責務分担

- **root default（`loop-protocol-personal-dev`）**: `extends = ":workspace"`（filesystem read は OS
  権限が許す範囲で制限しない、write は active workspace roots と system temporary directories に
  限定し、`.git`/`.agents`/`.codex` は protected metadata として write 不可）に加えて、明示的な
  development domain allowlist（`network.mode = "full"`、`allow_local_binding = false`、global
  `"*"` は使わない）を持つ。`:danger-full-access` を root default にはしない
- **agent-local profile（`.codex/agents/*.toml` の `default_permissions`）**: read-only custom agent は
  `loop-protocol-readonly`、write-capable custom agent は `loop-protocol-rtk` を明示宣言し続ける。
  root default の変更はこれらの agent-local 宣言を上書きしない
- PR #1849 の passive advisory recorder quarantine（`.codex/hooks.json` の `SessionEnd`/`SubagentStop`
  allowlist）は解除しない。command enforcement を repo hook（`PreToolUse`/`PermissionRequest`）へ
  戻すことは本 Issue の対象外

### Rollback（本 Issue の変更を戻す手順）

runtime smoke（`codex status` / App Server `skills/list` / 対話 TUI Skill refresh / custom agent
runtime smoke）が `loop-protocol-personal-dev` を root default にした状態で owner 承認済みの
filesystem/network semantics と異なる実効権限を示した場合、以下の手順で単一コミットとして戻す:

1. `git revert <この Issue の commit>` で `.codex/config.toml` の root
   `default_permissions = "loop-protocol-personal-dev"` 行と `[permissions.loop-protocol-personal-dev]`
   テーブル、`scripts/check-codex-agents.mjs` / `scripts/check_codex_agent_config.py` の root default
   assertion、`tests/codex/test_codex_hook_surface_guard.py` /
   `tests/codex/test_codex_permission_profile_config.py` の該当テスト、この節を含む
   `docs/dev/agent-runtime-ops.md` の更新を同一コミットで戻す
2. `pnpm exec node scripts/check-codex-agents.mjs --self-test` と
   `uv run python3 scripts/check_codex_agent_config.py --assert-required-fields --assert-runtime-contract --assert-local-main-branch-guard`
   が revert 後の状態（root default 不在、または built-in `:workspace` に戻った状態）でも意図どおり
   のメッセージで fail/pass することを確認する
3. rollback 後に root default が再び欠落した状態へ戻る場合、`codex status` は再び
   `does not set default_permissions` エラーを返すことを前提として運用者へ周知する
   （root default 不在への rollback は緊急停止としてのみ使う）
4. agent-local `default_permissions`（`loop-protocol-readonly` / `loop-protocol-rtk`）は本 rollback の
   対象外であり、変更しない

## WSL2 / standalone install の self-binary ENOENT 復旧（自己バイナリの復旧）

### 症状

WSL2 の `standalone install` で `~/.local/bin/codex` が `~/.codex/packages/standalone/.../bin/codex` を指しているとき、`codex sandbox linux` が sandbox 内で self-binary を再実行できず `self-binary ENOENT` になることがある。

```bash
codex sandbox linux -- pwd
# bwrap: execvp /home/<user>/.codex/packages/standalone/releases/<version>/bin/codex: No such file or directory
```

### 推奨 workaround

1. `~/.codex/packages/standalone/current/bin/codex` が指す実体を `/usr/local/bin/codex` に配置する
2. `PATH` で `/usr/local/bin` を `~/.local/bin` より前に置く
3. 以後の preflight は `/usr/local/bin/codex` が使われる状態で行う

例:

```bash
src="$(readlink -f "$HOME/.codex/packages/standalone/current/bin/codex")"
test -x "$src"
sudo install -m 0755 "$src" /usr/local/bin/codex

export PATH=/usr/local/bin:$PATH
which codex
readlink -f "$(which codex)"
codex --version
codex sandbox linux -- pwd
codex sandbox linux --permissions-profile loop-protocol-rtk -C . -- echo ok
```

### 更新時の注意

`/usr/local/bin/codex` は `~/.codex/packages/standalone/current` とは独立した実体コピーである。
Codex 更新後は version drift を避けるため、以下を必ず確認する。

```bash
which codex
readlink -f "$(which codex)"
/usr/local/bin/codex --version
~/.codex/packages/standalone/current/bin/codex --version
codex sandbox linux -- pwd
```

バージョンがずれている場合は、上記の `sudo install` を再実行して `/usr/local/bin/codex` を更新する。

### 定常運用で採らないもの

- `danger-full-access` を恒久運用の既定にしない
- `--sandbox workspace-write` を workaround の正解として固定しない
- `~/.codex` 全体の広い bind mount を定常解にしない
- `use_legacy_landlock` と permission profile の併用を前提にしない

`danger-full-access` は filesystem / network boundary を外すため、`self-binary ENOENT` の恒久解として採用しない。
切り分けで一時的に使った場合も、成功証跡は別途 `codex sandbox linux --permissions-profile ...` で取り直す。

### この repo で残してよい根拠

- Issue #350 で `~/.local/bin/codex` 経由の sandbox failure と `/usr/local/bin/codex` 優先での復旧を確認した
- PR #345 / Issue #343 の deferred AC6 は、この workaround を前提に `loop-protocol-rtk` で再確認する流れに整理されている

## Human Approval Load Reduction Policy（人間承認負荷の削減方針）

目標は、人間を **approval machine** にしないこと。
routine 操作は bounded な profile / rules / wrapper に寄せ、境界外だけ明示承認に残す。

### 既定

- `approval_policy = "on-request"` を既定にする
- `approval_policy = "never"` は既定にしない
- `danger-full-access` は既定にしない

### 低承認で寄せる操作

- `rtk gh`
- `rtk git`
- `rtk pnpm`
- 読み取り専用の調査（read-only inspection）
- repo 既定のVerification Commands

### 明示的に境界外として扱う操作

- `direct gh`
- `direct pnpm`
- `mutating git`
- `rtk curl`
- `rtk env`
- 新規 network 拡張
- secret / environment の広い参照
- sandbox bypass や runtime policy の再設計

## Network-Only Auto Allow（ネットワーク限定の自動許可）

この節は、Codex の repo-local 既定を「network だけを最小限 allow する」方向に寄せるための例を示す。
GitHub posting path 自体の正本は `docs/dev/github-ops.md` で、Codex session では `rtk gh` を low-approval boundary として使う。

### Modern profile example（現行プロファイル例）

```toml
approval_policy = "on-request"
default_permissions = "loop-protocol-personal-dev"

[permissions.loop-protocol-personal-dev]
extends = ":workspace"

[permissions.loop-protocol-personal-dev.network]
enabled = true
mode = "full"
allow_local_binding = false

[permissions.loop-protocol-personal-dev.network.domains]
"**.github.com" = "allow"

[permissions.loop-protocol-rtk.network]
enabled = true

[permissions.loop-protocol-rtk.network.domains]
"github.com" = "allow"
"api.github.com" = "allow"
"objects.githubusercontent.com" = "allow"
"uploads.github.com" = "allow"
```

この modern 例では、`default_permissions` と `[permissions.*]` だけを使い、filesystem 境界は
`extends = ":workspace"` の意味論から広げない。root default は `:workspace` を継承した
repository-defined custom profile `loop-protocol-personal-dev` であり、network allowlist を含む
`loop-protocol-rtk` は別途 agent-local に明示選択する custom profile である（root default とは責務が
分かれる、Issue #1859）。
GitHub への issue / PR 更新やコメント投稿は、`docs/dev/github-ops.md` の body-file guidance に従って `rtk gh` へ寄せる。

### Legacy compatibility note（旧設定との互換注記）

```toml
# legacy runtime only
[sandbox_workspace_write]
network_access = true
```

`network_access = true` は legacy 互換の説明であり、modern の `default_permissions = "loop-protocol-rtk"` と同じスニペットに混ぜない。
`sandbox_workspace_write` を使う場合でも、`danger-full-access` や `approval_policy = "never"` を既定にしない。

### GitHub Posting Boundary（GitHub 投稿の境界）

GitHub への issue / PR 更新、コメント投稿、draft PR 起票は `docs/dev/github-ops.md` を正本にし、`rtk gh` の low-approval boundary に寄せる。
`gh` 直叩きや `rtk curl` のような arbitrary network 操作は、この節の対象外とする。

## Official References（公式参照）

- Codex permissions（権限）: https://developers.openai.com/codex/permissions
- Codex rules（ルール）: https://developers.openai.com/codex/rules
- Codex AGENTS.md（指示ファイル）: https://developers.openai.com/codex/guides/agents-md
- Codex sandboxing / approval policy（サンドボックスと承認方針）: https://developers.openai.com/codex/concepts/sandboxing

## Permission / Rules / Instruction Surface（権限・ルール・指示面）

### project-local boundary（プロジェクトローカル境界）

- `.codex/config.toml` の root scope `default_permissions` は repository-defined custom profile `loop-protocol-personal-dev`（`extends = ":workspace"`: active workspace roots + system temporary directories への write、read は OS 権限が許す範囲、加えて明示的な development network allowlist）を選ぶ。`[permissions.*]` が非空のとき root default が欠落していると Codex loader（0.146.0 時点）が `config defines [permissions] profiles but does not set default_permissions` で拒否する（Issue #1859）
- root default は `.codex/agents/*.toml` の agent-local `default_permissions`（`loop-protocol-readonly` / `loop-protocol-rtk`）を上書きしない。custom agent は自身の profile を明示宣言し続ける
- `.codex/rules/default.rules` が command rules を持つ
- `AGENTS.md` が Codex 向けの project-local instruction surface になる
- `.agents/skills` は Git mode `120000` の root skill-directory symlink（link text: `../.claude/skills`）であり、Codex custom agent の repo-local discovery surface になる
- `.claude/skills/` は Claude 側 prompt / skill surface であり、上記 symlink の canonical package tree でもある
- regular directory、file symlink、absolute / repo 外 / broken / 誤った relative target は validator で fail-closed に拒否する
- topology 変更後は fresh process または明示的 reload で Codex / Claude Code の discovery を再実行し、inventory、重複なし、required file readback を linked worktree artifact に記録する
- installable artifact として配布したい場合は direct repo surface を増やさず plugin packaging を別 concern として扱う
- したがってこの PR 系列で揃えるのは「discovery surface の整合」であり、`.claude/skills/` 実体の全面移設までは主張しない

### 旧 sandbox 設定との競合

- legacy `sandbox_mode` と `--sandbox` は permission profile と競合しうる
- `default_permissions` を使う運用では、legacy `sandbox_mode` を steady-state に混在させない
- user-global config や CLI flag 側で `sandbox_mode` を有効化すると、repo 側の profile より旧挙動が優先されることがある

### trusted project 前提

- `.codex/config.toml` / `.codex/rules/*.rules` は project-local config layer の trust state に依存する
- `AGENTS.md` は Codex の instruction discovery 対象だが、読み込み確認は rules / config と分けて行う
- merge 後の利用者には「trusted project と active profile を自分の環境で確認すること」を前提として伝える

### 実効確認

```bash
codex status
codex execpolicy check --pretty --rules .codex/rules/default.rules -- rtk gh issue view 1
```

確認したい観点:

- active profile が `default_permissions` 想定どおりか
- `rules` surface が project-local 読み込みになっているか
- `instruction surface` として `AGENTS.md` / `CLAUDE.md` が読み込まれているか
- `codex status` 上で意図しない `sandbox_mode` や CLI override が残っていないか

## Codex 編集ガードの filesystem 境界（Native Permission Profile + Protected Paths Policy）

Issue #1612 により、`scripts/check-codex-agents.mjs` の write guard から旧 env 駆動モード
システム（モード選択用 env 変数 1 個・宣言的 allow-list 用 env 変数 1 個・既定モードへの
legacy boolean alias 1 個の計 3 個）は完全に除去された。編集時（apply_patch / Edit / Write）の
filesystem 境界は、現在は次の 2 つの独立した仕組みだけで構成される。いずれも env 変数を
authority として読み取らない。

1. **Native permission profile**（`.codex/config.toml` の
   `[permissions.<profile>.filesystem]`）: プロセス/サンドボックスレベルでの read/write を制御する。
2. **Protected paths policy の validated mirror**（`isProtectedPath()`、下記）: Issue の
   Allowed Paths に何が書かれていても、常に deny する保護領域を強制する。

Issue 単位で宣言された Allowed Paths そのものは、この編集時ガードでは**narrowing しない**
（Issue #1612 で意図的に廃止）。Allowed Paths の正本判定は PR review 時の独立した
`git diff` ベースの `allowed_paths_review_gate`（canonical）が担い、git staging/commit 自体は
`scripts/agent-guards/controlled_git_change_exec.py`（Issue #1611）が担う。旧モード用の env
変数を設定しても、この編集時ガードの判定には一切影響しない
（`scripts/agent-guards/tests/test_codex_legacy_env_ignored.py`、AC7 の negative test で
継続的に検証する）。

### 保護 path（常に deny）

- `assets/`
- `LICENSES/`
- `.env` および `.env.*`（リポジトリ内の任意の場所）
- `secrets/**`

これらの保護 path の**言語非依存の単一正本**は `scripts/agent-guards/protected_paths_policy.v1.json`
（`PROTECTED_PATHS_POLICY_V1`、`root_directory` / `basename_glob` の 2 種類の rule kind を持つ JSON）
である（Issue #1611、契約改訂）。`scripts/agent-guards/protected_paths_policy.py` はこの JSON を読み
込む Python loader（ハードコードなし）であり、`protected_paths_policy_sha256`（version 文字列では
なく JSON の生content の sha256）を公開する。`scripts/check-codex-agents.mjs` の `isProtectedPath()`
はこの JSON ファイルを直接読み込む（`--self-test` に mirror 整合の assertion がある）。
`.codex/config.toml` の `:workspace_roots` 読み取り専用エントリ（`assets`/`LICENSES`/`secrets`）は
JSON の `root_directory` rule の**部分的な** validated mirror である（`.env`/`.env.*` の
`basename_glob` rule はこの permission profile の文法では表現できないため、git staging/commit 層と
Codex write hook 層でのみ強制される）。`.claude/settings.json` の deny エントリ、
`controlled_git_change_exec.py` の staging/commit 判定も、この正本と意味的に一致する保護 path 集合
を維持する必要がある。protected path は Issue の Allowed Paths に明示されていても常に deny される
-- Allowed Paths は protected path へのアクセスを決して広げない。

## Controlled Stage/Commit Executor（統制済み stage/commit 実行機構、Issue #1611 の契約改訂で追加）

`scripts/agent-guards/controlled_git_change_exec.py` は、agent 駆動の git staging/commit を単一
transaction として所有する controlled executor である。`rtk git add` / `rtk git commit` / raw
`git add` / `git commit` シェルコマンド文字列は、この executor の外側では **常に deny** される
（`git_mutation_command_policy.py` の `classify_agent_lane_add_commit`、Issue #1611 AC9）。
`.codex/rules/default.rules` の `rtk git add` / `rtk git commit -m` ルールも `forbidden` に narrowing
されている（AC14）。この executor は、既存の `controlled_skill_mutation_exec.py`（Issue #1338 系）
と同水準の信頼境界（exact argv・repository/Issue binding・symlink/hardlink 拒否・環境変数
sanitization・canonical realpath・postcondition readback）を実装する。

**Threat model**: 本仕組みは cooperative agent workflow における誤操作・Issue scope 逸脱・無関係
変更混入を防ぐ repository-local guardrail であり、同一ユーザーの悪意ある process・
candidate-controlled policy 改変・OS-level adversary から独立した security boundary ではない。

### ISSUE_SCOPE_SNAPSHOT_V1

`build_issue_scope_snapshot()` が生成する private/local artifact で、以下を bind する（GitHub live
readback — issue body / comments — が前提であり、`issue_body` / `issue_updated_at` が空の場合は
fail-closed で `ValueError` になる）:

- `repository_full_name` / `issue_number`
- `contract_source_kind`（`issue_body` | `issue_comment`）/ `contract_source_id`
- `contract_source_body_sha256`（実際に採用した contract snapshot 本文の sha256）
- `issue_body_sha256`（現在の Issue 本文全体の sha256、contract source が comment の場合と分離）
- `issue_updated_at` / `comments_digest_sha256`（コメント本文の順序付き sha256、comment drift 検出用）
- `allowed_paths` / `allowed_paths_normalized_sha256` / `allowed_paths_matcher_schema`
- `base_ref` / `base_sha` / `branch_ref` / `worktree_realpath`
- `protected_paths_policy_schema` / `protected_paths_policy_sha256`（JSON の生content の sha256）
- `authority_mode`（下記の状態遷移）

### Live GitHub-bound materializer（live GitHub Issue から snapshot を実体化する materializer）

`scripts/agent-guards/materialize_issue_scope_snapshot.py` は、live Issue
readback と `CONTRACT_REVIEW_RESULT_V1 status: go` の comment URL を再読込して
snapshot を生成する唯一の producer である。呼出しは
`issue_scope_snapshot.materialize` command id を通し、入力 request の
repository / Issue / worktree / branch / default-base / output root を固定する。

出力は `artifacts/<issue>/issue-metadata/issue_scope_snapshot.materialize/` の
`issue_scope_snapshot.json` と hash-bound provenance sidecar の対である。
`ISSUE_SCOPE_SNAPSHOT_V1` 自体の key set は互換維持のため変更しない。
GitHub readback、contract source drift、base/worktree/branch binding、unsafe path のいずれかが
不成立なら artifact を残さず fail-closed とする。

**Issue #1629 fix_delta（provenance self-attestation の是正）**: snapshot
artifact と provenance sidecar はどちらもこの materializer 自身が書く、同一信頼領域の
出力である。したがって disk 上のこのペアが internally consistent であることは、
それ自体では認可根拠にならない（呼び出し元が両ファイルを手書きで一致させることを
妨げない）。`controlled_git_change_exec.py` はこの2ファイルを認可判定に一切使わない
—— consumer 契約は次のとおり:

- `controlled_git_change_exec.build_snapshot_via_live_materializer()` が、
  stage/commit と **同一 transaction 内で** materializer を in-process 呼び出しし、
  materializer が返す in-memory `snapshot` dict だけを `IssueScopeSnapshot` の
  構築に使う。disk 上の artifact / sidecar は監査証跡としてのみ書かれ、二度と
  読み返されない。
- materializer は `gh_bin`（trusted PATH から解決した実行ファイル）と、
  `GH_HOST` / `GH_REPO` / `GH_CONFIG_DIR` / `GIT_DIR` / `GIT_WORK_TREE` /
  `GIT_INDEX_FILE` / `GIT_OBJECT_DIRECTORY` / `GIT_ALTERNATE_OBJECT_DIRECTORIES`
  等を除去した sanitized env を明示的に受け取る（呼び出し元は
  `controlled_skill_mutation_exec._build_metadata_sanitized_env()` を使う）。
  ambient PATH の `gh` / 素の環境変数への fallback はない。
- contract source（`CONTRACT_REVIEW_RESULT_V1 status: go` comment）の検証は、
  substring 一致ではなく `.claude/skills/issue-contract-review/scripts/
  contract_review_result_parser.py` の canonical parser を再利用する
  （fenced YAML 構造解析・trusted publisher identity・
  `is_fingerprint_ready_go` による source-bound fingerprint 検証）。
  fingerprint の `base_ref` / `allowed_paths_normalized_sha256` は、この
  materialize() 呼び出しが独立に計算した値と一致することも要求する。
- default branch の名前と tip SHA は GitHub REST API
  （`gh api repos/<repo>` / `gh api repos/<repo>/git/ref/heads/<branch>`）
  から live に取得する。ローカルの `refs/heads/<base_ref>` は使わない。
- artifact / sidecar の書き込みは一時ファイル + 同一ファイルシステム上の
  `os.replace()` による atomic 置き換えであり、失敗時に torn/partial な
  artifact を残さない。
- `controlled_git_change_exec.py` の CLI は `--snapshot-json`（ファイル信頼
  パス）を無条件 deny する。唯一の入力経路は `--materialize-request`
  （`issue_number` / `repo` / `contract_snapshot_url` / `base_ref` /
  `branch_name` / `output` / `gh_bin` を含む JSON）で、これが上記の
  live in-process 呼び出しをトリガする。

### 実行ステップ（単一 transaction、「案B」: `git commit --only` + audit-then-rollback）

1. 認可 gate: `authority_mode == new_disabled_fail_closed` なら即座に deny（add/commit 停止）。
2. stale snapshot 検出（Issue body drift **または** comment drift、Allowed Paths drift）。
3. repository / worktree / cwd binding、detached HEAD / unborn branch / merge・rebase・cherry-pick
   進行中 / unmerged index を fail-closed で拒否し、branch/HEAD binding（race guard、`expected_head`
   は必須引数）を検証する。
4. 要求された pathspec が pathspec magic（`:( )` 等）や directory pathspec でない、literal な
   explicit path であることを検証する（AC5/AC6）。
5. staging 前に `git diff-index --cached --raw --full-index -z -M <EXPECTED_HEAD>`（bytes、NUL 区切り）
   で BASELINE を取得する（既存の無関係な staged 変更を記録しておく）。
6. `git --literal-pathspecs add --pathspec-from-file=- --pathspec-file-nul` で literal pathspec のみ
   を stage する（stdin は NUL 区切り bytes）。
7. 同じ oracle を再実行し、baseline との DELTA を計算する。DELTA が requested 集合と完全一致する
   ことを確認する（AC7）。rename の旧新 path・deletion・type change（old/new mode ベース）・
   submodule gitlink change（mode `160000`）を明示的に分類する（AC3/AC4、mode/OID 付き
   `ChangedFileRecord`、`changed_file_matcher.py` の共有 grammar を使用。rename-unaware な
   `--name-only` は認可 oracle として使わない）。
8. DELTA 対象パス（rename の旧新両方を含む）を `protected_paths_policy.py`（常に deny）と snapshot
   の Allowed Paths に照合する（AC2/AC3/AC4/AC10）。
9. `git commit --only --pathspec-from-file=- --pathspec-file-nul -m <message>` で commit する。
   `--only` が指定 path のみを commit 対象にするため、pre-existing の無関係な staged 変更は
   commit に巻き込まれない。
10. commit 後、`git diff-tree --no-commit-id -r --raw --full-index -z -M <commit_sha>`（commit の
    tree を親 tree と直接比較する。working tree との比較になる `diff-index`（`--cached` なし）は
    無関係な pre-existing drift を誤検出しうるため使わない）で commit 内容を再監査する。要求集合と
    不一致、または protected/Allowed Paths 違反が判明した場合は `git reset --soft HEAD~1` で
    自動 rollback し `status: deny` を返す。

commit hook（pre-commit/prepare-commit-msg/commit-msg）は `git commit --only` の通常の実行経路に
従い、実行される（暗黙のスキップはしない）。

### 環境変数の除去（env sanitization、P1-1）

子プロセスへ渡す環境から `GIT_DIR` / `GIT_COMMON_DIR` / `GIT_WORK_TREE` / `GIT_INDEX_FILE` /
`GIT_OBJECT_DIRECTORY` / `GIT_ALTERNATE_OBJECT_DIRECTORIES` / `GIT_CONFIG_SYSTEM` /
`GIT_CONFIG_GLOBAL` / `GIT_CONFIG_COUNT` / `GIT_EXEC_PATH` / `GIT_CEILING_DIRECTORIES` を除去する
（`_sanitized_git_env()`、すべての git subprocess 呼び出しに適用）。

### Concurrency（残存 race の明記）

本 executor は stage-to-commit の race window を**狭めるが、完全には排除しない**。staging 直前と
commit 直前の両方で local HEAD を再確認するが、private `GIT_INDEX_FILE` + `git update-ref` の
compare-and-swap transaction（「案A」）は実装していない（Out of Scope として明示的に不採用） --
単一 worktree に対して controlled executor の呼び出しが並行しない運用モデルを前提とした判断であり、
過剰と判断した設計上のトレードオフである（Issue #1611 In Scope）。複数呼び出しが同一 worktree に
対して同時実行され得る場合、pre-commit HEAD 再確認と実際の `git commit` 呼び出しの間に残存 race
がある。

### Authority Mode 状態遷移（旧 env と新 snapshot の非同時 authority）

`resolve_authority()` は `CODEX_ALLOWED_PATHS` 等の旧 env と `ISSUE_SCOPE_SNAPSHOT_V1` が同時に
authority にならないことを保証する純粋な決定コアである。`authority_mode` は以下の 4 状態を持つ:

| authority_mode | authoritative_source | 説明 |
|---|---|---|
| `old_only` | legacy env | 旧 env のみが staging/commit の可否を決定する |
| `migration_validation` | legacy env | 旧 env が引き続き唯一の enforcement authority。snapshot は並行して計算・比較されるが enforcement には使わない（audit only） |
| `new_only` | snapshot | `ISSUE_SCOPE_SNAPSHOT_V1` のみが authority になる |
| `new_disabled_fail_closed` | なし（none） | 緊急停止状態。add/commit を停止し、旧 env authority への**自動 fallback は行わない**。復旧は明示的な release rollback 手続きとして扱う（`execute_controlled_change()` はこの状態で即座に `authority_new_disabled_fail_closed_add_commit_stopped` を返す） |

`authoritative_source` は `authority_mode` のみに基づいて決定され、legacy env / snapshot の
どちらが実際に present かには依存しない -- そのため両方が present な状態でも、決して blend
されず、常にどちらか一方だけが enforcement を担う（Issue #1611 AC12）。契約改訂前の
`rollback_to_old` という状態名・自動 fallback を示唆する表現は廃止された。

### Rollback（障害時の安全側フォールバック、Issue #1612 AC9）

`isProtectedPath()` は `scripts/agent-guards/protected_paths_policy.v1.json` を毎プロセス起動時に
一度読み込み（`loadProtectedPathsPolicy()`）、スキーマ不一致・空 `rules`・JSON parse 失敗を
`throw` で fail-closed に扱う。この例外は catch されないため、PreToolUse hook（`--hook-pretool`）
のプロセス自体が非ゼロ終了する。Codex の hook runner は hook プロセスの異常終了を
「deny 決定を返せなかった hook」として扱うため、結果的に該当 apply_patch/Edit/Write は
実行されない（silent allow にはならない）。

障害発生時（例: JSON SSOT が壊れた・permission profile 定義自体が壊れた）の復旧手順:

1. `git diff HEAD -- scripts/agent-guards/protected_paths_policy.v1.json .codex/config.toml`
   で直近の変更を確認する。
2. `git checkout HEAD -- scripts/agent-guards/protected_paths_policy.v1.json` （または
   `.codex/config.toml`）で直前の既知良好状態へ戻す。この 2 ファイルはどちらも Issue #1612 の
   設計では env override を持たないため、**env 変数による緊急バイパスは存在しない**
   （旧モード env システム時代のような「モードを切り替えて回避する」手段は意図的に
   廃止された— fail-closed を維持するための設計判断）。
3. `node scripts/check-codex-agents.mjs --self-test` で `PROTECTED_PATHS_POLICY_V1 JSON SSOT
   mirrors` セクションと `protected-path enforcement` セクションが全 PASS することを確認してから
   Codex セッションを再開する。
4. 復旧後も編集を続行できない場合は、native permission profile 側（`.codex/config.toml` の
   `default_permissions` を `loop-protocol-readonly` に切り替える）で Codex agent を読み取り専用に
   固定し、人間判断を待つ。これは旧 strict モード相当の「宣言なしは
   全 deny」という以前の fail-closed 挙動と同じ安全側（deny-heavy）の状態を、env 変数ではなく
   `.codex/config.toml` の恒久設定として再現する。

### PermissionRequest の remote_write 緩和

`codex-hook-adapter.mjs` は `PermissionRequest` イベントで `remote_write_requires_approval`（git push 系）を
`behavior: deny` ではなく no_decision（stdout なし・exit 0）として扱う。
`PreToolUse` 側は引き続き `behavior: deny` を返す（方針 B）。
`secret_boundary_violation`・`forbidden_path`・`public_checkpoint`・`secrets_mode` は
`PermissionRequest` でも `deny` を維持する。

## Worktree Agent Runtime Smoke（実機起動による動作検証。Issue #1887）

Claude Code／Codex CLI の runtime smoke（linked worktree 内での fresh session 起動と observation）は
`.claude/skills/worktree-agent-runtime-smoke/SKILL.md` に集約する。structured lane（既定、非対話
`claude -p` / `codex exec --json --ephemeral`、常に direct subprocess）と interactive herdr lane
（TUI 固有挙動の観測が必要な場合のみ、常に呼び出し元とは分離した isolated named session）の
2 lane を持つ。herdr は `mode=interactive` にのみ必須であり、structured lane は herdr を一切
経由しない。runner は起動・観測・証跡収集だけを所有し、Codex CLI 側の
sandbox／permission profile（本文書の Root Default Permission Profile）は変更しない。

## Cross References（相互参照）

- GitHub 操作の共通規約: [github-ops.md](github-ops.md)
- 実装フローの正本: [workflow.md](workflow.md)
- 既知の背景: Issue #350, Issue #343, PR #345
