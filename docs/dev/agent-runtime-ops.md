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
default_permissions = "loop-protocol-rtk"

[permissions.loop-protocol-rtk.network]
enabled = true

[permissions.loop-protocol-rtk.network.domains]
"github.com" = "allow"
"api.github.com" = "allow"
"objects.githubusercontent.com" = "allow"
"uploads.github.com" = "allow"
```

- この例は filesystem boundary を広げず、network allowlist の差分だけを示す
- `default_permissions` と `[permissions.*]` だけを使い、legacy `sandbox_workspace_write` には依存しない
- `uploads.github.com` は release asset / upload 系の経路に限定して追加している
- `loop-protocol-readonly` と `loop-protocol-bootstrap` には追加しない。どちらも upload / release asset の許可を必要としないため、read-only / bootstrap の境界を狭く保つ

### Legacy compatibility note（旧設定との互換注記）

```toml
# legacy runtime only
[sandbox_workspace_write]
network_access = true
```

- `network_access = true` は legacy runtime のみの表現で、modern `default_permissions` と混在させない
- GitHub issue / PR updates and comments still use [github-ops.md](github-ops.md) and `rtk gh`

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
- read-only inspection
- repo 既定の検証コマンド

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
default_permissions = "loop-protocol-rtk"

[permissions.loop-protocol-rtk.network]
enabled = true

[permissions.loop-protocol-rtk.network.domains]
"github.com" = "allow"
"api.github.com" = "allow"
"objects.githubusercontent.com" = "allow"
"uploads.github.com" = "allow"
```

この modern 例では、`default_permissions` と `[permissions.*]` だけを使い、filesystem 境界は広げない。
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

- `.codex/config.toml` の `default_permissions` が repo 既定 profile を選ぶ
- `.codex/rules/default.rules` が command rules を持つ
- `AGENTS.md` が Codex 向けの project-local instruction surface になる
- `.agents/skills/` が Codex custom agent の repo-local discovery surface になる
- `.claude/skills/` は Claude 側 prompt / skill surface であり、現時点では thin bridge が読む canonical body の保管場所でもある
- repo-local authoring/discovery surface は `.agents/skills/` を discovery、`.claude/skills/` を canonical body として分ける
- Codex 公式の `symlinked skill folders` support は確認済みだが、この repo では symlink portability is unproven; thin bridge is the default
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

## CODEX_ALLOWED_PATHS_MODE

`scripts/check-codex-agents.mjs` の write guard は `CODEX_ALLOWED_PATHS_MODE` 環境変数でモードを制御する。

### モード一覧

| モード | 説明 | 既定 |
|---|---|---|
| `workspace` | repo workspace 内の通常編集を allow。保護 path は常に deny。`CODEX_ALLOWED_PATHS` が設定されている場合は intersect（narrowing）で絞り込む。 | **既定（未設定時）** |
| `strict` | `CODEX_ALLOWED_PATHS` の明示宣言が必須。未宣言時は全書き込みを deny（fail-closed）。 | — |
| `shadow` | workspace と同じ allow ロジック + would-block イベントを `.guard_shadow_log.jsonl`（repo root, git 管理外）に記録。 | — |
| `unknown`（上記以外） | fail-closed（全書き込みを deny）。 | — |

### 保護 path（全モード共通・常に deny）

- `assets/`
- `LICENSES/`
- `.env` および `.env.*`（リポジトリ内の任意の場所）
- `secrets/**`

これらの保護 path の**言語非依存の単一正本**は `scripts/agent-guards/protected_paths_policy.py`
（`PROTECTED_PATHS_POLICY_V1`）である（Issue #1611）。`scripts/check-codex-agents.mjs` の write guard、
`.claude/settings.json` の deny エントリ、`controlled_git_change_exec.py` の staging/commit 判定は、
この正本と意味的に一致する保護 path 集合を維持する必要がある（validated mirror）。protected path は
Issue の Allowed Paths に明示されていても常に deny される -- Allowed Paths は protected path への
アクセスを決して広げない。

## Controlled Stage/Commit Executor（Issue #1611）

`scripts/agent-guards/controlled_git_change_exec.py` は、agent 駆動の git staging/commit を単一
transaction として所有する controlled executor である。`rtk git add` / `rtk git commit` / raw
`git add` / `git commit` シェルコマンド文字列は、この executor の外側では **常に deny** される
（`git_mutation_command_policy.py` の `classify_agent_lane_add_commit`、Issue #1611 AC9）。

### ISSUE_SCOPE_SNAPSHOT_V1

`build_issue_scope_snapshot()` が生成する private/local artifact で、以下を bind する:

- Issue body の `body_sha256`
- Allowed Paths を正規化した `allowed_paths_normalized_sha256`
- `base_branch` / `base_sha`
- `worktree_realpath`（`os.path.realpath` による worktree の実パス）
- `protected_paths_policy_version`（`PROTECTED_PATHS_POLICY_V1` のバージョン文字列）
- `authority_version`（下記の状態遷移）

### 実行ステップ（単一 transaction）

1. repository / worktree / branch / HEAD binding を検証する（stale snapshot / race 検出を含む）。
2. 要求された pathspec が pathspec magic（`:( )` 等）や directory pathspec でない、literal な
   explicit path であることを検証する（AC5/AC6）。
3. literal pathspec のみを単一の `git add -- <paths...>` で stage する。
4. `git diff --cached --name-status -M -z` で index を再取得する（rename-unaware な `--name-only`
   は認可 oracle として使わない）。rename の旧新 path・deletion・type change・submodule gitlink
   change を明示的に分類する（AC3/AC4、`changed_file_matcher.py` の共有 grammar を使用）。
5. 監査対象パス（rename の旧新両方を含む）を `protected_paths_policy.py`（常に deny）と snapshot
   の Allowed Paths に照合する（AC2/AC3/AC4/AC10）。
6. staged 集合と requested 集合が完全一致することを確認する（AC7、不一致は fail-closed deny）。
7. commit し、commit 後の diff を再監査する。post-commit audit が pre-commit audit と食い違う場合
   （設計上到達しないはずの defense-in-depth）、`git reset --soft HEAD^` で commit を rollback し
   deny する。

### Concurrency（残存 race の明記）

本 executor は stage-to-commit の race window を**狭めるが、完全には排除しない**。staging 直前と
commit 直前の両方で local HEAD を再確認し、stage 前に index が空（既存 staged 変更がない）ことを
要求するが、private `GIT_INDEX_FILE` + `git update-ref` の compare-and-swap trasaction は実装して
いない -- 単一 worktree に対して controlled executor の呼び出しが並行しない運用モデルを前提とした
判断であり、過剰と判断した設計上のトレードオフである（Issue #1611 In Scope）。複数呼び出しが同一
worktree に対して同時実行され得る場合、pre-commit HEAD 再確認と実際の `git commit` 呼び出しの間に
残存 race がある。

### Authority Version 状態遷移（旧 env と新 snapshot の非同時 authority）

`resolve_authority()` は `CODEX_ALLOWED_PATHS` 等の旧 env と `ISSUE_SCOPE_SNAPSHOT_V1` が同時に
authority にならないことを保証する純粋な決定コアである。`authority_version` は以下の 4 状態を持つ:

| authority_version | authoritative_source | 説明 |
|---|---|---|
| `old_only` | legacy env | 旧 env のみが staging/commit の可否を決定する |
| `migration_validation` | legacy env | 旧 env が引き続き唯一の enforcement authority。snapshot は並行して計算・比較されるが enforcement には使わない（audit only） |
| `new_only` | snapshot | `ISSUE_SCOPE_SNAPSHOT_V1` のみが authority になる |
| `rollback_to_old` | legacy env | `new_only` からの明示的な rollback。旧 env に戻す |

`authoritative_source` は `authority_version` のみに基づいて決定され、legacy env / snapshot の
どちらが実際に present かには依存しない -- そのため両方が present な状態でも、決して blend
されず、常にどちらか一方だけが enforcement を担う（Issue #1611 AC12）。

### 設定方法

```bash
# workspace モードを既定にする（Codex セッション起動時に設定）
export CODEX_ALLOWED_PATHS_MODE=workspace

# shadow モード（観測用）
export CODEX_ALLOWED_PATHS_MODE=shadow

# strict モード（明示 path のみ allow）
export CODEX_ALLOWED_PATHS_MODE=strict
export CODEX_ALLOWED_PATHS="scripts
docs/dev"
```

### 推奨構成

- **CI / 自動化**: `strict` モード（`CODEX_ALLOWED_PATHS` を Issue contract から設定する）
- **インタラクティブ開発**: `workspace` モード
- **新規 guard ルール検証**: `shadow` モード（`.guard_shadow_log.jsonl` を確認）

### CODEX_LEGACY_ALLOW_WRITES との関係

`CODEX_LEGACY_ALLOW_WRITES=1` は後方互換のために残すが、内部的に `workspace` モードと同等に統合される。
新規設定では `CODEX_ALLOWED_PATHS_MODE=workspace` を明示的に使うことを推奨する。

### PermissionRequest の remote_write 緩和

`codex-hook-adapter.mjs` は `PermissionRequest` イベントで `remote_write_requires_approval`（git push 系）を
`behavior: deny` ではなく no_decision（stdout なし・exit 0）として扱う。
`PreToolUse` 側は引き続き `behavior: deny` を返す（方針 B）。
`secret_boundary_violation`・`forbidden_path`・`public_checkpoint`・`secrets_mode` は
`PermissionRequest` でも `deny` を維持する。

## Cross References（相互参照）

- GitHub 操作の共通規約: [github-ops.md](github-ops.md)
- 実装フローの正本: [workflow.md](workflow.md)
- 既知の背景: Issue #350, Issue #343, PR #345
