# Agent Runtime Ops

Codex local runtime 運用の主文書。
この文書は Codex CLI の起動・sandbox・permission profile・rules・instruction surface を扱い、GitHub 操作ルールそのものは [github-ops.md](github-ops.md) を参照する。

## Runtime Positioning

- Codex CLI は **optional runtime** であり、`Claude Code`、既存 `SSOT`、既存 `workflow` を置き換えない
- repo の正本は引き続き `CLAUDE.md`、`docs/dev/workflow.md`、`docs/product/requirements.md` などの SSOT 群にある
- Codex 向けの project-local guidance は `AGENTS.md` に集約し、この文書はその runtime 前提と復旧手順を補足する

## Network-Only Auto Allow

この節は **network boundary の差分例** であり、permission profile 全体を列挙する complete profile ではない。
`uploads.github.com` は GitHub の release asset / upload 系 endpoint 向けで、issue / PR の投稿やコメントの主経路ではない。
GitHub issue / PR の更新・コメントは引き続き [github-ops.md](github-ops.md) と `rtk gh` を使う。

### Modern example

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

### Legacy compatibility note

```toml
# legacy runtime only
[sandbox_workspace_write]
network_access = true
```

- `network_access = true` は legacy runtime のみの表現で、modern `default_permissions` と混在させない
- GitHub issue / PR updates and comments still use [github-ops.md](github-ops.md) and `rtk gh`

## WSL2 / standalone install の self-binary ENOENT 復旧

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

## Human Approval Load Reduction Policy

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

## Network-Only Auto Allow

この節は、Codex の repo-local 既定を「network だけを最小限 allow する」方向に寄せるための例を示す。
GitHub posting path 自体の正本は `docs/dev/github-ops.md` で、Codex session では `rtk gh` を low-approval boundary として使う。

### Modern profile example

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

### Legacy compatibility note

```toml
# legacy runtime only
[sandbox_workspace_write]
network_access = true
```

`network_access = true` は legacy 互換の説明であり、modern の `default_permissions = "loop-protocol-rtk"` と同じスニペットに混ぜない。
`sandbox_workspace_write` を使う場合でも、`danger-full-access` や `approval_policy = "never"` を既定にしない。

### GitHub Posting Boundary

GitHub への issue / PR 更新、コメント投稿、draft PR 起票は `docs/dev/github-ops.md` を正本にし、`rtk gh` の low-approval boundary に寄せる。
`gh` 直叩きや `rtk curl` のような arbitrary network 操作は、この節の対象外とする。

## Official References

- Codex permissions: https://developers.openai.com/codex/permissions
- Codex rules: https://developers.openai.com/codex/rules
- Codex AGENTS.md: https://developers.openai.com/codex/guides/agents-md
- Codex sandboxing / approval policy: https://developers.openai.com/codex/concepts/sandboxing

## Permission / Rules / Instruction Surface

### project-local boundary

- `.codex/config.toml` の `default_permissions` が repo 既定 profile を選ぶ
- `.codex/rules/default.rules` が command rules を持つ
- `AGENTS.md` が Codex 向けの project-local instruction surface になる

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

## Cross References

- GitHub 操作の共通規約: [github-ops.md](github-ops.md)
- 実装フローの正本: [workflow.md](workflow.md)
- 既知の背景: Issue #350, Issue #343, PR #345
