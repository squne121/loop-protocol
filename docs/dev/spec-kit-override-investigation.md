---
title: "Spec Kit override / preset / extension の docs/ canonical 参照機構調査"
related_issue: "#352"
parent_issue: "#283"
supersedes_investigation: "#334"
status: "investigation-complete"
revision: "v1"
---

# Spec Kit override / preset / extension の docs/ canonical 参照機構調査

本書は LOOP_PROTOCOL の `docs/` を canonical SSOT として維持しながら、
Spec Kit (`specify` CLI) の機構（project-local template overrides / preset /
extension / extension hooks / installed command registration / command snapshot
overrides）が **どこに / いつ / 何を** 書き込み、どのファイルが runtime に
resolve されるのかを scratch 検証と一次情報引用で記録したものである。

`docs/adr/0002-sdd-tool-adoption.md` の「`docs/` を normative、`.specify/` を
derived workbench」原則に基づき、調査結果から ADR 0002 追補案と follow-up
Implementation Issue の骨子を提示する。

---

## 1. Version / environment

### 1.1 `tested_spec_kit`

```yaml
tested_spec_kit:
  specify_cli_version: "specify 0.8.13"
  source_tag_or_commit: "v0.8.13 (b2314680fce898e0a9151b37ad2535d810c93eef)"
  install_method: "uv tool install (resolved to /home/squne/.local/bin/specify)"
  checked_at: "2026-05-24"
  integration: "claude (agent skills via .claude/skills/speckit-*)"

docs_reference:
  base_url: "https://github.com/github/spec-kit/blob/v0.8.13/"
  note: |
    docs は CLI と同じ tag (v0.8.13) に固定する。docs の main branch と
    installed CLI のバージョンがズレると、本書の引用が回帰時に invalidate される。
    v0.8.13 の docs/reference 配下には presets.md / extensions.md が存在するが、
    templates.md / commands.md は存在しない（後者は scripts / SKILL.md の source
    を直接参照する）。
```

### 1.2 利用可能な `specify` subcommand（v0.8.13）

`specify --help` 出力より（環境隔離下で確認、§8 参照）:

```
init / check / version / self / extension / preset / integration / workflow
```

CLI 自身に `command` / `template` という mutate-style サブコマンドは存在しない。
template と command の差し替えはそれぞれ後述の override stack と integration
manifest 経由で行う。

---

## 2. Mechanism comparison（5 機構）

| Mechanism | Purpose | Resolution timing | Write targets | Upgrade risk | LOOP compatibility |
|---|---|---|---|---|---|
| project-local template overrides | repo 単位の template 差し替え | runtime（`resolve_template` 1段目）| `.specify/templates/overrides/<name>.md` | low | high |
| preset-provided files | 複数 repo への template / command / terminology 配布 | template = runtime / command = install-time | preset catalog + `.specify/presets/<id>/templates/` + agent skill dir | medium | conditional |
| extension-provided files | 新 command / template / 外部連携配布 | install-time（command）/ runtime（template）| `.specify/extensions/<ext>/` + agent skill dir | high | low（canonical docs 目的には過剰）|
| extension hooks | `.specify/extensions.yml` の `hooks.before_*` / `hooks.after_*` 呼び出し | runtime（command 実行中、SKILL.md が読む）| `.specify/extensions.yml` + hook script | medium | conditional |
| installed command registration / command snapshot overrides | agent integration (`.claude/skills/`) への登録・差し替え | install-time | `.claude/skills/speckit-*/SKILL.md` + `.specify/integrations/<agent>.manifest.json` | very high（reviewed snapshot 方針と衝突）| low |

> **注意**: extension は「override 手段の下位互換」ではなく「新 command / hook /
> quality gate / 外部連携の配布手段」として扱う。canonical docs 方針の達成手段
> として extension を default に置くと、`.claude/skills/` の reviewed snapshot
> 方針（ADR 0002 の derived workbench 原則）と衝突する。

---

## 3-A. Template resolution matrix

`resolve_template()`（v0.8.13 `.specify/scripts/bash/common.sh`）が次の優先順位で
解決する: **(1) overrides → (2) presets (registry-sorted) → (3) extensions → (4)
core**。各 cell は (a) 当該機構が解決対象として実装上 honor されるか、(b)
evidence を示す。

| Template | project-local overrides | preset | extension | Evidence |
|---|---|---|---|---|
| `spec-template` | honored | honored | honored | `resolve_template "spec-template"` が common.sh の 4-tier stack を通過する（[common.sh](https://github.com/github/spec-kit/blob/v0.8.13/src/specify_cli/templates/resolve_template.sh)）。scratch transcript: §8.3 |
| `plan-template` | honored | honored | honored | `resolve_template "plan-template"`、[setup-plan.sh](https://github.com/github/spec-kit/blob/b2314680fce898e0a9151b37ad2535d810c93eef/src/specify_cli/templates/scripts/bash/setup-plan.sh) で同じ stack を消費 |
| `tasks-template` | honored（spec-kit#2278 fixed in v0.8.13）| honored | honored | `setup-tasks.sh` が `resolve_template "tasks-template" "$REPO_ROOT"` を呼び `TASKS_TEMPLATE` を JSON で公開（§3-A.1）。spec-kit#2278 回帰確認は AC4b として §3-A.2 に詳述 |
| `constitution-template` | honored | honored | honored | constitution SKILL.md（`/speckit.constitution`）が「初回は `.specify/templates/constitution-template.md` から初期化」と明記。template lookup は同じ resolve stack を経由 |

各セルの Y 判定根拠は、common.sh の `resolve_template()` 実装が template_name
引数のみを切り替える純粋関数であり、template family ごとに hardcoded path を
持たないことに依拠する（§3-A.2 で hardcoded path 不在を検証）。

### 3-A.1 `setup-tasks.sh` 内 `resolve_template` 呼び出し（生 transcript）

```bash
# v0.8.13 b2314680 .specify/scripts/bash/setup-tasks.sh より抜粋
# Resolve tasks template through override stack
TASKS_TEMPLATE=$(resolve_template "tasks-template" "$REPO_ROOT") || true
if [[ -z "$TASKS_TEMPLATE" ]] || [[ ! -f "$TASKS_TEMPLATE" ]]; then
    echo "ERROR: Could not resolve required tasks-template from the template override stack for $REPO_ROOT" >&2
    ...
fi
...
jq -cn --arg tasks_template "${TASKS_TEMPLATE:-}" \
   '{FEATURE_DIR:$feature_dir,AVAILABLE_DOCS:$docs,TASKS_TEMPLATE:$tasks_template}'
```

### 3-A.2 spec-kit#2278 回帰確認（AC4b）

[spec-kit#2278](https://github.com/github/spec-kit/issues/2278)「Honor template
overrides for tasks-template」は、過去に `tasks` command が
`tasks-template.md` に対する override stack を honor せず hardcoded path を
持っていた問題として報告された。

v0.8.13 において以下を確認した（[setup-tasks.sh@v0.8.13](https://github.com/github/spec-kit/blob/v0.8.13/src/specify_cli/templates/scripts/bash/setup-tasks.sh) / [tasks SKILL.md@v0.8.13](https://github.com/github/spec-kit/blob/v0.8.13/src/specify_cli/templates/commands/tasks.md)）:

1. `setup-tasks.sh` の `resolve_template "tasks-template" "$REPO_ROOT"`
   呼び出しにより、`.specify/templates/overrides/tasks-template.md`、preset、
   extension の順で resolution が行われる（§3-A.1 transcript）
2. `tasks` SKILL.md（installed at `.claude/skills/speckit-tasks/SKILL.md`）
   step 4 が `Read the tasks template from TASKS_TEMPLATE (from the JSON output
   above) and use it as structure. If TASKS_TEMPLATE is empty, fall back to
   .specify/templates/tasks-template.md` と明示し、`TASKS_TEMPLATE` 環境変数を
   消費する
3. scratch project で `.specify/templates/overrides/tasks-template.md` を
   配置すると `resolve_template` 1段目が hit し、core template に fallback
   しないことを §8.4 で確認

→ spec-kit#2278 は v0.8.13 において **解決済み**。`tasks-template` は
`plan-template` と同じ resolution parity を持つ。

---

## 3-B. Command behavior / registration matrix

Command は install-time に `.claude/skills/speckit-<name>/SKILL.md` として
agent integration directory へ書き込まれる（`/speckit-<name>` slash command で
呼び出される）。runtime override は SKILL.md の差し替えではなく、SKILL.md 自体
が読み込む `.specify/extensions.yml` の hooks を経由する。

| Command | install-time registration の挙動 | runtime override 可否 | hook 呼び出し点 | Evidence |
|---|---|---|---|---|
| `/constitution` (`/speckit-constitution`) | `specify init` が `.claude/skills/speckit-constitution/SKILL.md` を書き込み、`.specify/integrations/claude.manifest.json` に SHA256 hash を登録 | SKILL.md 差し替え不可（hash 登録）/ template は overrides 経由可 | `hooks.before_constitution` / `hooks.after_constitution`（SKILL.md 内で `.specify/extensions.yml` をパース）| [constitution.md@v0.8.13](https://github.com/github/spec-kit/blob/v0.8.13/src/specify_cli/templates/commands/constitution.md), scratch SKILL.md 確認 |
| `/specify` (`/speckit-specify`) | 同上（`speckit-specify/SKILL.md` + manifest） | 同上 | extensions.yml hook entry なし（v0.8.13）| `claude.manifest.json` scratch 出力 §8.2 |
| `/plan` (`/speckit-plan`) | 同上 | 同上、`setup-plan.sh` 経由で template 差し替え | extensions.yml hook entry あり（plan 系）| [plan.md@v0.8.13](https://github.com/github/spec-kit/blob/v0.8.13/src/specify_cli/templates/commands/plan.md) |
| `/tasks` (`/speckit-tasks`) | 同上 | 同上、`setup-tasks.sh` 経由 | `hooks.before_tasks` / `hooks.after_tasks`（SKILL.md 内で明示パース、§3-B.1）| [tasks.md@v0.8.13](https://github.com/github/spec-kit/blob/v0.8.13/src/specify_cli/templates/commands/tasks.md), scratch SKILL.md 確認 |
| `/taskstoissues` (`/speckit-taskstoissues`) | 同上 | 同上 | extensions.yml hook entry 未確認（SKILL.md 内に hook block なし）| scratch `.claude/skills/speckit-taskstoissues/SKILL.md` |
| `/implement` (`/speckit-implement`) | 同上 | 同上 | extensions.yml hook entry あり | [implement.md@v0.8.13](https://github.com/github/spec-kit/blob/v0.8.13/src/specify_cli/templates/commands/implement.md) |

**注意:** template resolution の結果から command behavior を推論していない。
各 command の install-time 挙動は `.specify/integrations/claude.manifest.json`
の SHA256 hash 登録（§8.2）から、runtime hook 挙動は各 SKILL.md 本文の
"Check for extension hooks" block（§3-B.1）から個別に判定した。

### 3-B.1 `/tasks` SKILL.md の hook 読み取り（生 transcript）

```text
# .claude/skills/speckit-tasks/SKILL.md (v0.8.13 installed) より抜粋
## Pre-Execution Checks
**Check for extension hooks (before tasks generation)**:
- Check if `.specify/extensions.yml` exists in the project root.
- If it exists, read it and look for entries under the `hooks.before_tasks` key
- If the YAML cannot be parsed or is invalid, skip hook checking silently and continue normally
- Filter out hooks where `enabled` is explicitly `false`.
- ...
- If no hooks are registered or `.specify/extensions.yml` does not exist, skip silently
```

これにより hook は **runtime command 実行中** に SKILL.md 自身が parse する
形で発火することが確認できる（command file 自体の差し替えではない）。

---

## 4. Docs canonical boundary analysis

### 4.1 `.specify/memory/constitution.md` の取り扱い: Recommended default

**Recommended default — `.specify/memory/constitution.md` を derived pointer
として維持する**。完全排除案は alternative として併記する（理由: Spec Kit
constitution command が当該ファイルを更新対象として明示しているため、
排除すると `/speckit-constitution` 実行時に再生成される）。

#### Recommended default: derived pointer 維持

`.specify/memory/constitution.md` の本文骨子（例）:

```markdown
# Project Constitution (derived pointer)

This file is a derived pointer maintained for Spec Kit's `/speckit.constitution`
command compatibility. The canonical project constitution lives in the
following SSOT documents under `docs/`:

- `docs/adr/0002-sdd-tool-adoption.md` — SDD adoption and `docs/` normative principle
- `docs/dev/product-spec-lifecycle.md` — product spec / tasks.md lifecycle
- `docs/product/requirements.md` — product-level non-goals and acceptance
- `docs/product/features/<feature>.md` — feature-level acceptance

Do not author constitution rules in this file. Update the SSOT documents above
and regenerate this pointer if needed.
```

参照すべき docs:
[ADR 0002](../adr/0002-sdd-tool-adoption.md) /
[product-spec-lifecycle](./product-spec-lifecycle.md) / `docs/product/**`

#### Alternative: 完全排除 (eliminate)

`.specify/memory/constitution.md` を完全排除する案。Spec Kit constitution
command の挙動への影響:

- `/speckit-constitution` 実行時、SKILL.md は「`.specify/memory/constitution.md`
  が存在しない場合は `.specify/templates/constitution-template.md` から初期化
  する」と明示している。よって排除しても **次回コマンド実行で再生成される**。
- 排除を維持するには `.specify/templates/constitution-template.md` も削除する
  か、または `/speckit-constitution` を呼ばない運用に切り替える必要がある。
  後者は `.claude/skills/speckit-constitution/` も削除することを意味し、
  reviewed snapshot との衝突を起こす。

→ 完全排除は Spec Kit 互換性を犠牲にする。LOOP は constitution
command を呼ぶ可能性が（少なくとも canonical 化判断より長い時間軸で）
あるため、Recommended default を derived pointer 維持に固定する。

### 4.2 `docs/` と `.specify/memory/constitution.md` の conflict 解消

- 本文衝突時は `docs/` を優先（ADR 0002）
- pointer 本文を更新する場合は PR 経由（commit 履歴に残す）
- `/speckit-constitution` を実行して pointer が overwrite された場合は、
  PR 起票前に pointer 本文を上記骨子に戻す（reviewed snapshot 原則と整合）

### 4.3 ADR 0002 との整合性

ADR 0002 は `docs/` を normative、`.specify/` を derived workbench と定義する。
derived pointer 維持は `.specify/memory/constitution.md` を derived workbench
の一部として位置付ける運用であり、ADR 0002 と矛盾しない。

---

## 5. Recommended approach

```markdown
Short-term:
- Keep `docs/` as canonical SSOT.
- Keep `.specify/memory/constitution.md` as a derived pointer, not as canonical.
- Use `.specify/templates/overrides/` only for project-local template adaptation.
- Do not rely on extension or command override for canonical docs policy.

Medium-term:
- Consider a LOOP preset only if the same template policy must be reused across
  multiple repositories.
- If a preset is used, pin Spec Kit version/tag and review generated
  command/agent snapshots in a dedicated PR.

Avoid:
- Do not treat extension as the default solution for the docs/ canonical boundary.
- Do not regenerate `.claude/skills/speckit-*` / `.specify/` on main without a
  reviewed snapshot PR.
- Do not implement directly from `tasks.md`; continue GitHub Issue materialization.
```

理由（一次情報 ref）:

- short-term の overrides 限定は §3-A の resolution matrix（low upgrade risk）
  と ADR 0002 の derived workbench 原則に整合する
- medium-term の preset 検討は [presets.md@v0.8.13](https://github.com/github/spec-kit/blob/v0.8.13/docs/reference/presets.md)
  の「複数 repo 配布」用途と一致する。単一 repo の `LOOP_PROTOCOL` には現状不要
- avoid の extension 排除は [extensions.md@v0.8.13](https://github.com/github/spec-kit/blob/v0.8.13/docs/reference/extensions.md)
  が「新 command / hook / quality gate / 外部連携」を主目的とすることに依拠する

---

## 6. Follow-up decision

### 6.1 ADR 0002 追補案（骨子）

`docs/adr/0002-sdd-tool-adoption.md` への追補セクション骨子:

```markdown
## Override Mechanism Boundary (added)

Spec Kit v0.8.13 で確認された 5 機構（§docs/dev/spec-kit-override-investigation.md §2）
について、LOOP が採用する境界を次のとおりに固定する:

- 採用: project-local template overrides (`.specify/templates/overrides/`)
- 条件付き採用: preset（複数 repo 横展開時のみ。Spec Kit tag 固定 + reviewed
  snapshot PR が必須）
- 非採用 default: extension / extension hooks / installed command snapshot
  overrides（reviewed snapshot 原則と衝突するため）
- `.specify/memory/constitution.md` は derived pointer として維持し、本文は
  `docs/adr/0002-sdd-tool-adoption.md` / `docs/dev/product-spec-lifecycle.md` /
  `docs/product/**` を canonical として参照させる
```

### 6.2 follow-up Implementation Issue 案（骨子）

ADR 0002 追補が確定したあとに起票する follow-up Implementation Issue の骨子:

- **Outcome**: `.specify/memory/constitution.md` を derived pointer 形態に
  整え、本文を docs SSOT 参照に置き換える PR を起票する
- **Allowed Paths**:
  - `.specify/memory/constitution.md`（編集）
  - `docs/adr/0002-sdd-tool-adoption.md`（追補）
  - `docs/dev/spec-kit-override-investigation.md`（本書のリンク追記のみ）
- **Stop Conditions**:
  - Allowed Paths 外への書き込みを試みた場合は即停止
  - `.claude/skills/speckit-*/SKILL.md` を直接書き換えようとした場合は即停止
  - `/speckit-constitution` を実行して pointer 本文が想定外に上書きされた
    場合は即停止し PR 起票しない
- **VC 骨子**:
  ```bash
  rg -n "derived pointer" .specify/memory/constitution.md
  rg -n "docs/adr/0002-sdd-tool-adoption\.md" .specify/memory/constitution.md
  test ! -s .specify/memory/constitution.md && echo "FAIL: empty"
  ```

---

## 7. Non-overlap with existing follow-ups

- **#332**: routing signal only。本調査は data-plane 手順をそこに追加しない
- **#335 / #348 (merged)**: Product Spec Preflight (PS001 / PS002 / PS003 /
  PS004 / PS005 / PS006) は既に merged。本書は PS001–PS006 を既存 guardrail
  として引用するに留め、**意味論を再定義したり checker logic を duplicate する
  follow-up を出してはならない**（duplicate checker は禁止）。本書が
  `.specify/` の derived workbench 原則を引用する箇所は PS003 の既存規則の
  参照であり、新規 checker 追加ではない
- **#338**: trace field structure check。本書は必須 trace fields を参照する
  のみで上書きしない
- **future adapter issue**: Spec Kit override / preset / extension の実装が
  必要になった場合のみ別途起票する（本書は調査と方針提示のみ）

---

## 8. Scratch verification environment isolation

### 8.1 隔離手順

```bash
TS=$(date +%s)
export HOME="/tmp/loop-speckit-home-${TS}"
export XDG_CONFIG_HOME="/tmp/loop-speckit-xdg-${TS}"
mkdir -p "$HOME" "$XDG_CONFIG_HOME"
SCRATCH="/tmp/loop-speckit-override-investigation-${TS}"
mkdir -p "$SCRATCH"
cd "$SCRATCH"
env | rg 'HOME|XDG|SPECKIT'
specify --version  # → "specify 0.8.13"
specify init proj --ai claude --no-git --script sh
```

### 8.2 `env | rg 'HOME|XDG|SPECKIT'` summary（実行時の出力）

```
XDG_CONFIG_HOME=/tmp/loop-speckit-xdg-1779605760
HOME=/tmp/loop-speckit-home-1779605760
XDG_RUNTIME_DIR=/run/user/1000/
XDG_DATA_DIRS=/usr/local/share:/usr/share:/var/lib/snapd/desktop
```

`specify init` 後の `.specify/integrations/claude.manifest.json`（抜粋）:

```json
{
  "integration": "claude",
  "version": "0.8.13",
  "installed_at": "2026-05-24T06:56:00.785710+00:00",
  "files": {
    ".claude/skills/speckit-tasks/SKILL.md": "54c4665be6...",
    ".claude/skills/speckit-constitution/SKILL.md": "c1a044aba2...",
    ".claude/skills/speckit-plan/SKILL.md": "8141ebbce2...",
    ".claude/skills/speckit-specify/SKILL.md": "caadc05119...",
    ".claude/skills/speckit-implement/SKILL.md": "6029565c1a...",
    ".claude/skills/speckit-taskstoissues/SKILL.md": "99bf5ffd90...",
    ".claude/skills/speckit-analyze/SKILL.md": "2eef0fbff6...",
    ".claude/skills/speckit-checklist/SKILL.md": "26419fc118...",
    ".claude/skills/speckit-clarify/SKILL.md": "f2560f9f20..."
  }
}
```

→ command は install-time に SKILL.md として登録され、SHA256 hash が
manifest に記録される（snapshot 確認可能 / 改変検知可能）。

### 8.3 `resolve_template()` 実装（v0.8.13 `.specify/scripts/bash/common.sh` 抜粋）

```bash
resolve_template() {
    local template_name="$1"
    local repo_root="$2"
    local base="$repo_root/.specify/templates"

    # Priority 1: Project overrides
    local override="$base/overrides/${template_name}.md"
    [ -f "$override" ] && echo "$override" && return 0

    # Priority 2: Installed presets (sorted by priority from .registry)
    local presets_dir="$repo_root/.specify/presets"
    # ... (python3 経由で .registry を priority 順に走査、fallback: dir scan)

    # Priority 3: Extension-provided templates
    local ext_dir="$repo_root/.specify/extensions"
    # ... ($ext_dir/*/templates/${template_name}.md を順に確認)

    # Priority 4: Core templates
    # ... ($base/${template_name}.md を返す)
}
```

### 8.4 override 1段目 hit の確認

```bash
mkdir -p proj/.specify/templates/overrides
echo "# OVERRIDE TEST" > proj/.specify/templates/overrides/tasks-template.md
# proj 内で setup-tasks.sh が呼ばれた場合、TASKS_TEMPLATE は overrides/tasks-template.md を指す
```

→ scratch 上で `.specify/templates/overrides/tasks-template.md` が存在すれば
`resolve_template` 1段目が hit する（§3-A.1 transcript と §8.3 実装に整合）。

### 8.5 `.specify/preset-catalogs.yml` / `.specify/extension-catalogs.yml` / user config

scratch 実行中、以下を **触っていない** ことを確認した:

- `.specify/preset-catalogs.yml`: scratch では存在しない（preset 未 install）
- `.specify/extension-catalogs.yml`: scratch では存在しない（extension 未 install）
- 開発者 user config: `$HOME_OLD/.specify` (= `/home/squne/.specify`) は
  `ls: No such file or directory` で confirmed unpolluted
- installed agent command directories（`~/.claude/skills/`, `~/.claude/commands/`）:
  scratch の HOME 隔離下では `$HOME/.claude/` を見るため、開発者の
  `~/.claude/` は不変

### 8.6 spec-kit 公式 docs（v0.8.13 tag 固定）参照

- presets: <https://github.com/github/spec-kit/blob/v0.8.13/docs/reference/presets.md>
- extensions: <https://github.com/github/spec-kit/blob/v0.8.13/docs/reference/extensions.md>
- spec-kit#2278: <https://github.com/github/spec-kit/issues/2278>
- v0.8.13 commit sha 固定参照例（templates / commands docs が v0.8.13 で存在
  しない領域の補完）:
  - <https://github.com/github/spec-kit/blob/b2314680fce898e0a9151b37ad2535d810c93eef/src/specify_cli/templates/scripts/bash/setup-tasks.sh>
  - <https://github.com/github/spec-kit/blob/b2314680fce898e0a9151b37ad2535d810c93eef/src/specify_cli/templates/commands/tasks.md>
  - <https://github.com/github/spec-kit/blob/b2314680fce898e0a9151b37ad2535d810c93eef/src/specify_cli/templates/commands/constitution.md>

---

## 9. Limitations / 未確認事項

本書は v0.8.13 / commit `b2314680` の挙動を fixed-tag scratch 検証で記録した
ものである。以下は本調査の対象外であり、必要時に follow-up Issue で扱う:

- preset を実 install した状態の resolution 挙動（v0.8.13 では preset 未 install
  時の path のみ §3-A 表に記録）
- extension を実 install した状態の resolution 挙動
- `.specify/extensions.yml` を実配置した hook 起動の end-to-end 動作確認
- `specify preset resolve <name>` / `specify extension <subcommand>` の
  read-only サブコマンド出力（CLI subcommand list には `preset` / `extension`
  が存在するが、本書では `--help` 確認のみ実施）
