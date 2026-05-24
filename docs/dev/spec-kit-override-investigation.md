---
title: "Spec Kit override / preset / extension の docs/ canonical 参照機構調査"
related_issue: "#352"
parent_issue: "#283"
supersedes_investigation: "#334"
status: "investigation-complete"
revision: "v2"
---

# Spec Kit override / preset / extension の docs/ canonical 参照機構調査

本書は LOOP_PROTOCOL の `docs/` を canonical SSOT として維持しながら、
Spec Kit (`specify` CLI) v0.8.13 の 5 機構（project-local template overrides /
preset / extension / extension hooks / installed command registration & command
snapshot overrides）が **どこに / いつ / 何を** 書き込み、どのファイルが
runtime に resolve されるのかを scratch 検証と一次情報引用で記録したものである。

`docs/adr/0002-sdd-tool-adoption.md` の「`docs/` を normative、`.specify/` を
derived workbench」原則に基づき、調査結果から ADR 0002 追補案と 2 本の
follow-up Implementation Issue 案（ADR 追補 + pointer 実装、Depends on 関係）の
骨子を提示する。

> **本書は live Spec Kit capability statement ではない**。プロジェクトが
> v0.8.13 を超えてアップグレードする際は、§3-A / §3-B の matrix と spec-kit#2278
> の回帰確認（§3-A.2）を必ず rerun すること。Spec Kit は更新が速く、release
> 後に main へ追加 commit が積まれる前提で、docs / CLI ともに **同一 tag に
> 固定**しないと evidence が壊れる。

---

## 1. Version / environment

### 1.1 `tested_spec_kit`

```yaml
tested_spec_kit:
  specify_cli_version: "specify 0.8.13"
  source_tag_or_commit: "v0.8.13 (b2314680fce898e0a9151b37ad2535d810c93eef)"
  install_method: "uv tool install (resolved to $REAL_HOME/.local/bin/specify)"
  checked_at: "2026-05-24"
  integration: "claude (agent skills via .claude/skills/speckit-*)"

docs_reference:
  base_url: "https://github.com/github/spec-kit/blob/v0.8.13/"
  note: |
    docs は CLI と同じ tag (v0.8.13) に固定する。docs の main branch と
    installed CLI のバージョンがズレると、本書の引用が回帰時に invalidate される。
    Spec Kit の release page (v0.8.13, 2026-05-21) では、release 後にも main へ
    追加 commit が積まれていることが確認できる。よって本書の URL はすべて
    v0.8.13 tag または commit b2314680 固定で記載する。
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

### 2.1 Evidence level の定義

§3-A / §3-B の matrix セルは以下の `evidence_type` を併記する:

- **scratch-e2e**: 環境隔離下の scratch project で実装が観測通り動作したことを
  transcript で確認した（§8.4 参照）
- **source-inspected**: v0.8.13 tag の upstream source を読み、実装上は honor
  される（または規約が記述されている）ことを確認した
- **not-tested**: 本書ではテストしていない（後続調査の余地）

scratch-e2e を “source の resolver 対応” と混同しないため、各セルは
`<judgement> by <evidence_type>` の形で記載する。

---

## 3-A. Template resolution matrix

`resolve_template()`（v0.8.13 [`scripts/bash/common.sh`](https://github.com/github/spec-kit/blob/v0.8.13/scripts/bash/common.sh)）
が次の優先順位で解決する: **(1) overrides → (2) presets (registry-sorted) →
(3) extensions → (4) core**。各セルは「honored / not honored」と
`evidence_type`（scratch-e2e / source-inspected / not-tested）を併記する。

| Template | project-local overrides | preset | extension | Evidence |
|---|---|---|---|---|
| `spec-template` | honored by source-inspected | honored by source-inspected | **honored by scratch-e2e (single synthetic extension filesystem placement)** | `.specify/extensions/test-ext/templates/spec-template.md` を配置し `resolve_template spec-template "$PWD"` が当該 path を返すことを確認（§8.4）。`specify extension add` 等の install flow / catalog / priority registry / enable-disable / 複数 extension の precedence は §3-A.3 caveat / §9 Limitations 参照（**not-tested**） |
| `plan-template` | honored by source-inspected | honored by source-inspected | honored by source-inspected | [`scripts/bash/setup-plan.sh`@v0.8.13](https://github.com/github/spec-kit/blob/v0.8.13/scripts/bash/setup-plan.sh) が同じ `resolve_template` を消費 |
| `tasks-template` | honored by source-inspected | **honored by scratch-e2e** | honored by source-inspected | `setup-tasks.sh` が `resolve_template "tasks-template" "$REPO_ROOT"` を呼び `TASKS_TEMPLATE` を JSON で公開（§3-A.1）。preset E2E: `.specify/presets/test-preset/templates/tasks-template.md` が priority 1 で hit（§8.4）。spec-kit#2278 回帰確認は AC4b として §3-A.2 に詳述 |
| `constitution-template` | **honored by scratch-e2e** | honored by source-inspected | honored by source-inspected | `.specify/templates/overrides/constitution-template.md` を配置し `resolve_template constitution-template` が override path を返すことを確認（§8.4）。constitution SKILL.md は「初回は `.specify/templates/constitution-template.md` から初期化」と明記し、template lookup は同じ resolve stack を経由 |

各セルの判定根拠は、`resolve_template()` 実装が `template_name` 引数のみを
切り替える純粋関数であり、template family ごとに hardcoded path を持たないこと
（§3-A.2 で hardcoded path 不在を spec-kit#2278 回帰として検証）に依拠する。
scratch-e2e セルは §8.4 の synthetic preset / extension / override 配置で
4 層 stack のすべての priority が観測通り hit することを確認している。

### 3-A.1 `setup-tasks.sh` 内 `resolve_template` 呼び出し（生 transcript）

```bash
# v0.8.13 https://github.com/github/spec-kit/blob/v0.8.13/scripts/bash/setup-tasks.sh より抜粋
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

v0.8.13 において以下を確認した（[`scripts/bash/setup-tasks.sh`@v0.8.13](https://github.com/github/spec-kit/blob/v0.8.13/scripts/bash/setup-tasks.sh) /
[`templates/commands/tasks.md`@v0.8.13](https://github.com/github/spec-kit/blob/v0.8.13/templates/commands/tasks.md)）:

1. `setup-tasks.sh` の `resolve_template "tasks-template" "$REPO_ROOT"`
   呼び出しにより、`.specify/templates/overrides/tasks-template.md`、preset、
   extension の順で resolution が行われる（§3-A.1 transcript）
2. `tasks` SKILL.md（installed at `.claude/skills/speckit-tasks/SKILL.md`）
   step 4 が `Read the tasks template from TASKS_TEMPLATE (from the JSON output
   above) and use it as structure. If TASKS_TEMPLATE is empty, fall back to
   .specify/templates/tasks-template.md` と明示し、`TASKS_TEMPLATE` 環境変数を
   消費する
3. scratch project で `.specify/presets/test-preset/templates/tasks-template.md`
   を配置すると `resolve_template "tasks-template"` が preset path を返し、
   core template に fallback しないことを §8.4 で確認

→ spec-kit#2278 は v0.8.13 において **解決済み**。`tasks-template` は
`plan-template` と同じ resolution parity を持つ（fixed evidence: §3-A.1 の
`setup-tasks.sh` source / §8.4 の scratch E2E / spec-kit#2278 issue 本文）。

### 3-A.3 Extension template precedence caveat（docs / source 差分）

v0.8.13 の公式 [`docs/reference/presets.md`@v0.8.13](https://github.com/github/spec-kit/blob/v0.8.13/docs/reference/presets.md)
は file resolution stack で extension layer を「Installed extensions — sorted
by priority」と説明する。一方、本書が依拠する v0.8.13
[`scripts/bash/common.sh::resolve_template()`](https://github.com/github/spec-kit/blob/v0.8.13/scripts/bash/common.sh)
の source inspection では、**extension layer は単純な directory glob で走査**
されている（preset layer のみが `.registry` を python3 経由で priority sort
する）:

```bash
# 抜粋 (v0.8.13 scripts/bash/common.sh resolve_template Priority 3)
# Priority 3: Extension-provided templates
local ext_dir="$repo_root/.specify/extensions"
if [ -d "$ext_dir" ]; then
    for ext in "$ext_dir"/*/; do
        [ -d "$ext" ] || continue
        # Skip hidden directories (e.g. .backup, .cache)
        case "$(basename "$ext")" in .*) continue;; esac
        local candidate="$ext/templates/${template_name}.md"
        [ -f "$candidate" ] && echo "$candidate" && return 0
    done
fi
```

つまり、**この関数内では extension priority registry を読んでいる証跡はない**。
本書の §8.4 scratch-e2e は single synthetic extension（`test-ext` のみ）の
hit 確認であり、**multiple extensions with same template name を配置した
場合の precedence は本書では not-tested**。

LOOP は extension を canonical docs 方針の default 手段にしない（§2 / §5
Avoid）ため、この docs / source 差分は本書の採用方針を変えない。ただし、
future adapter issue で extension を採用検討する場合は、

- 上記 `resolve_template_content()` も含む `common.sh` 全体の extension
  resolution 実装を v0.8.13 + その時点の latest 双方で再走査する
- multiple extensions のテストケースを scratch project で組む（同名 template
  を 2 つ以上の extension に置き、どちらが選ばれるかを transcript 化）
- 公式 docs と source の差分を tag 単位で記録する

を必須とする。

---

## 3-B. Command behavior / registration matrix

Command は install-time に `.claude/skills/speckit-<name>/SKILL.md` として
agent integration directory へ書き込まれる（`/speckit-<name>` slash command で
呼び出される）。**runtime override は SKILL.md 自体の差し替えではなく**、SKILL.md
が読み込む `.specify/extensions.yml` の hooks を経由する。

| Command | install-time registration の挙動 | runtime override 可否 | hook 呼び出し点 | Evidence |
|---|---|---|---|---|
| `/constitution` (`/speckit-constitution`) | `specify init` が `.claude/skills/speckit-constitution/SKILL.md` を書き込み、`.specify/integrations/claude.manifest.json` に SHA256 hash を登録 | not supported by template resolution stack（template 側のみ overrides 経由可）。local mutation は可能だが reviewed snapshot drift として扱う（hash は drift detection に使え、enforcement lock ではない） | `hooks.before_constitution` / `hooks.after_constitution`（SKILL.md 内で `.specify/extensions.yml` をパース、source-inspected） | [`templates/commands/constitution.md`@v0.8.13](https://github.com/github/spec-kit/blob/v0.8.13/templates/commands/constitution.md), scratch SKILL.md 確認（§8.2） |
| `/specify` (`/speckit-specify`) | 同上（`speckit-specify/SKILL.md` + manifest） | 同上 | scratch SKILL.md 内に hook block 未確認（v0.8.13 init 直後 default） | `claude.manifest.json` scratch 出力 §8.2 |
| `/plan` (`/speckit-plan`) | 同上 | 同上、template は `setup-plan.sh` 経由で差し替え可 | hook block source-inspected（plan 系） | [`templates/commands/plan.md`@v0.8.13](https://github.com/github/spec-kit/blob/v0.8.13/templates/commands/plan.md) |
| `/tasks` (`/speckit-tasks`) | 同上 | 同上、template は `setup-tasks.sh` 経由で差し替え可 | `hooks.before_tasks` / `hooks.after_tasks`（SKILL.md 内で明示パース、§3-B.1, scratch-e2e read）| [`templates/commands/tasks.md`@v0.8.13](https://github.com/github/spec-kit/blob/v0.8.13/templates/commands/tasks.md), scratch SKILL.md 確認 |
| `/taskstoissues` (`/speckit-taskstoissues`) | 同上 | 同上 | scratch SKILL.md 内に extensions.yml hook block 未確認（not-tested for end-to-end） | scratch `.claude/skills/speckit-taskstoissues/SKILL.md` |
| `/implement` (`/speckit-implement`) | 同上 | 同上 | hook block source-inspected | [`templates/commands/implement.md`@v0.8.13](https://github.com/github/spec-kit/blob/v0.8.13/templates/commands/implement.md) |

**注意:** template resolution の結果から command behavior を推論していない。
各 command の install-time 挙動は `.specify/integrations/claude.manifest.json`
の SHA256 hash 登録（§8.2）から、runtime hook 挙動は各 SKILL.md 本文の
"Check for extension hooks" block（§3-B.1）から個別に判定した。
**“差し替え不可” ではなく “runtime stack では差し替わらない / reviewed snapshot
drift として扱う”** が正確な表現である（manifest hash は file system mutation を
物理的に防止しない）。

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
- medium-term の preset 検討は [`docs/reference/presets.md`@v0.8.13](https://github.com/github/spec-kit/blob/v0.8.13/docs/reference/presets.md)
  の「複数 repo 配布」用途と一致する。単一 repo の `LOOP_PROTOCOL` には現状不要
- avoid の extension 排除は [`docs/reference/extensions.md`@v0.8.13](https://github.com/github/spec-kit/blob/v0.8.13/docs/reference/extensions.md)
  が「新 command / hook / quality gate / 外部連携」を主目的とし、Spec Kit
  maintainers が独立作者の preset / extension を review / audit / endorse /
  support しないと明記していることに依拠する（supply-chain guard は §10 参照）

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
  snapshot PR + §10 supply-chain guard 遵守が必須）
- 非採用 default: extension / extension hooks / installed command snapshot
  overrides（reviewed snapshot 原則と衝突するため）
- `.specify/memory/constitution.md` は derived pointer として維持し、本文は
  `docs/adr/0002-sdd-tool-adoption.md` / `docs/dev/product-spec-lifecycle.md` /
  `docs/product/**` を canonical として参照させる
```

### 6.2 follow-up Implementation Issue 案（2 本に分割）

ADR 決定と pointer 実装を 1 Issue = 1 PR 原則に合わせて **2 本に分割** する
（順序: A → B）。

#### Follow-up A: ADR 0002 追補（policy 決定のみ）

- **Outcome**: `docs/adr/0002-sdd-tool-adoption.md` に Override Mechanism
  Boundary セクション（§6.1 骨子）を追加し、5 機構の採用境界を固定する
- **Allowed Paths**:
  - `docs/adr/0002-sdd-tool-adoption.md`（追補編集）
  - `docs/dev/spec-kit-override-investigation.md`（追補リンクの追記のみ可）
- **Stop Conditions**:
  - Allowed Paths 外への書き込みを試みた場合は即停止
  - `.specify/` / `.claude/skills/` への永続書き込みを試みた場合は即停止
  - 既存 ADR 0002 の本文を policy decision なく書き換えた場合は即停止
  - 着手時に installed `specify --version` が v0.8.13 以外の場合は即停止し、
    `docs/dev/spec-kit-override-investigation.md` の §3-A / §3-B matrix と
    §3-A.2 spec-kit#2278 regression を **rerun** したうえで本書を新 tag に
    更新するまで ADR 追補を進めない
- **VC 骨子**:
  ```bash
  rg -n "Override Mechanism Boundary" docs/adr/0002-sdd-tool-adoption.md
  rg -n "project-local template overrides" docs/adr/0002-sdd-tool-adoption.md
  rg -n "derived pointer" docs/adr/0002-sdd-tool-adoption.md
  ```

#### Follow-up B: `.specify/memory/constitution.md` derived pointer 化（実装）

- **Depends on**: Follow-up A（ADR 0002 追補が main にマージされていること）
- **Outcome**: `.specify/memory/constitution.md` の本文を §4.1 Recommended
  default の骨子に置き換え、derived pointer として運用する状態にする
- **Allowed Paths**:
  - `.specify/memory/constitution.md`（編集）
- **Stop Conditions**:
  - Allowed Paths 外への書き込みを試みた場合は即停止
  - `.claude/skills/speckit-*/SKILL.md` を直接書き換えようとした場合は即停止
  - `/speckit-constitution` を実行して pointer 本文が想定外に上書きされた
    場合は即停止し PR 起票しない
  - Follow-up A がマージされていない場合は即停止（順序違反）
  - 着手時に installed `specify --version` が v0.8.13 以外の場合は即停止し、
    `docs/dev/spec-kit-override-investigation.md` の §3-A / §3-B matrix と
    §3-A.2 spec-kit#2278 regression を **rerun** したうえで constitution
    command の挙動（pointer の上書き挙動含む）を新 tag で再確認するまで
    pointer 実装を進めない
- **VC 骨子**:
  ```bash
  rg -n "derived pointer" .specify/memory/constitution.md
  rg -n "docs/adr/0002-sdd-tool-adoption\.md" .specify/memory/constitution.md
  test -s .specify/memory/constitution.md && echo "PASS: non-empty"
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
hash は drift detection に利用できるが、file system mutation を物理的に防止
するものではない（よって command matrix の表現は “runtime stack では差し替わ
らない / reviewed snapshot drift として扱う”）。

### 8.3 `resolve_template()` 実装（v0.8.13 [`scripts/bash/common.sh`](https://github.com/github/spec-kit/blob/v0.8.13/scripts/bash/common.sh) 抜粋）

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

### 8.4 4 層 stack の E2E hit 確認（synthetic preset / extension / override）

scratch project に各 priority 用の synthetic template を配置し、
`resolve_template` がどの path を返すかを直接確認した:

```bash
# scratch init 後の追加配置（HOME/XDG 隔離下）
mkdir -p proj/.specify/presets/test-preset/templates
echo '{"presets":{"test-preset":{"priority":1,"enabled":true}}}' \
  > proj/.specify/presets/.registry
echo "# PRESET TASKS TEMPLATE" \
  > proj/.specify/presets/test-preset/templates/tasks-template.md

mkdir -p proj/.specify/extensions/test-ext/templates
echo "# EXTENSION SPEC TEMPLATE" \
  > proj/.specify/extensions/test-ext/templates/spec-template.md

mkdir -p proj/.specify/templates/overrides
echo "# OVERRIDE CONSTITUTION" \
  > proj/.specify/templates/overrides/constitution-template.md

cd proj
source .specify/scripts/bash/common.sh
echo "[OVERRIDE]:  $(resolve_template constitution-template "$PWD")"
echo "[PRESET]:    $(resolve_template tasks-template      "$PWD")"
echo "[EXTENSION]: $(resolve_template spec-template       "$PWD")"
echo "[CORE]:      $(resolve_template plan-template       "$PWD")"
```

実行結果（実 transcript）:

```
[OVERRIDE]:  /tmp/loop-speckit-e2e-<TS>/proj/.specify/templates/overrides/constitution-template.md
[PRESET]:    /tmp/loop-speckit-e2e-<TS>/proj/.specify/presets/test-preset/templates/tasks-template.md
[EXTENSION]: /tmp/loop-speckit-e2e-<TS>/proj/.specify/extensions/test-ext//templates/spec-template.md
[CORE]:      /tmp/loop-speckit-e2e-<TS>/proj/.specify/templates/plan-template.md
```

→ override / preset / extension / core の 4 段すべての priority が
**観測通り** hit することを確認（§3-A 表の scratch-e2e セル根拠）。
synthetic preset / extension は scratch project の `.specify/presets/` /
`.specify/extensions/` 配下に置いただけで、開発者環境 (`$REAL_HOME/.specify`)
には触れていない（§8.5 の before/after guard 参照）。

### 8.5 開発者環境 mutation の before/after guard

scratch 検証中に開発者の `$REAL_HOME` 配下が一切 mutate されていないことを
before/after diff で確認する手順:

```bash
REAL_HOME="${REAL_HOME:-$(getent passwd "$(whoami)" | cut -d: -f6)}"
before_specify="$(find "$REAL_HOME/.specify" -maxdepth 2 -type f 2>/dev/null | sort || true)"
before_claude="$(find "$REAL_HOME/.claude/skills" -maxdepth 2 -type f 2>/dev/null | sort || true)"

# ... scratch verification (§8.1–§8.4) ...

after_specify="$(find "$REAL_HOME/.specify" -maxdepth 2 -type f 2>/dev/null | sort || true)"
after_claude="$(find "$REAL_HOME/.claude/skills" -maxdepth 2 -type f 2>/dev/null | sort || true)"
diff <(printf '%s\n' "$before_specify") <(printf '%s\n' "$after_specify") \
  && echo "PASS: no .specify pollution" \
  || echo "FAIL: .specify modified"
diff <(printf '%s\n' "$before_claude") <(printf '%s\n' "$after_claude") \
  && echo "PASS: no .claude/skills pollution" \
  || echo "FAIL: .claude/skills modified"
```

本書の検証実行（2026-05-24）では:

- `$REAL_HOME/.specify`: 検証前後ともに「存在しない」（`ls: cannot access '...': No such file or directory`）
- `$REAL_HOME/.claude/skills/speckit-tasks`: 検証前後ともに「存在しない」
- → before/after diff は空 = pollution なし

`$REAL_HOME` を経由した記述にすることで、個人環境固有の絶対パス（例:
`/home/squne`）に依存しない、再現可能な検証手順とした。

### 8.6 `.specify/preset-catalogs.yml` / `.specify/extension-catalogs.yml` / user config

scratch 実行中、以下を **触っていない** ことを確認した:

- `.specify/preset-catalogs.yml`: scratch では存在しない（preset 未 install /
  synthetic preset のみ scratch project 内で作成）
- `.specify/extension-catalogs.yml`: scratch では存在しない（extension 未
  install / synthetic extension のみ scratch project 内で作成）
- 開発者 user config: `$REAL_HOME/.specify` は §8.5 の guard で confirmed
  unpolluted
- installed agent command directories: §8.5 の guard で confirmed unpolluted

### 8.7 spec-kit 公式 docs（v0.8.13 tag / commit 固定）参照

- presets: <https://github.com/github/spec-kit/blob/v0.8.13/docs/reference/presets.md>
- extensions: <https://github.com/github/spec-kit/blob/v0.8.13/docs/reference/extensions.md>
- spec-kit#2278: <https://github.com/github/spec-kit/issues/2278>
- v0.8.13 source 固定参照（本書の matrix evidence で利用）:
  - <https://github.com/github/spec-kit/blob/v0.8.13/scripts/bash/common.sh>
  - <https://github.com/github/spec-kit/blob/v0.8.13/scripts/bash/setup-tasks.sh>
  - <https://github.com/github/spec-kit/blob/v0.8.13/scripts/bash/setup-plan.sh>
  - <https://github.com/github/spec-kit/blob/v0.8.13/templates/commands/tasks.md>
  - <https://github.com/github/spec-kit/blob/v0.8.13/templates/commands/constitution.md>
  - <https://github.com/github/spec-kit/blob/v0.8.13/templates/commands/plan.md>
  - <https://github.com/github/spec-kit/blob/v0.8.13/templates/commands/implement.md>
- commit sha 固定参照（同一内容、上記 tag URL の immutable 補完）:
  - <https://github.com/github/spec-kit/blob/b2314680fce898e0a9151b37ad2535d810c93eef/scripts/bash/setup-tasks.sh>
  - <https://github.com/github/spec-kit/blob/b2314680fce898e0a9151b37ad2535d810c93eef/templates/commands/tasks.md>

> v0.8.13 の `docs/reference/` 配下には presets.md / extensions.md は存在するが
> `templates.md` / `commands.md` は存在しない。よって template / command の
> 挙動説明は本書では (a) `scripts/bash/*.sh` および `templates/commands/*.md`
> の source、および (b) scratch 観測結果から直接引用する。

---

## 9. Limitations / 未確認事項

本書は v0.8.13 / commit `b2314680` の挙動を fixed-tag scratch 検証で記録した
ものである。以下は本調査の対象外であり、必要時に follow-up Issue で扱う:

- **preset / extension の synthetic を超えた検証**: §8.4 で synthetic preset /
  extension / override 各 1 件の resolution stack hit を確認した。実 install
  flow（registry catalog 経由 / network install / 公開 preset の参照）の検証は
  本書では行っていない（**not-tested**）
- **extension hook end-to-end**: SKILL.md が `.specify/extensions.yml` を
  parse することは source-inspected（§3-B.1）。実 hook script を配置して
  command 実行中に hook が発火する end-to-end 動作の確認は本書では行って
  いない（**not-tested**）
- **`specify preset <subcommand>` / `specify extension <subcommand>` の read
  系出力**: CLI subcommand list には存在するが、本書では `--help` 確認のみ
  実施。subcommand 単位の挙動（例: `specify preset resolve <name>`）の
  E2E transcript は別途必要な場合に取得する
- **upgrade beyond v0.8.13**: 本書は live capability statement ではない。
  Spec Kit upgrade 時は §3-A / §3-B の matrix と §3-A.2 の spec-kit#2278
  regression を rerun し、本書を新 tag に更新する必要がある

---

## 10. Supply-chain guard for preset / extension adoption

Spec Kit の公式 [`docs/reference/presets.md`@v0.8.13](https://github.com/github/spec-kit/blob/v0.8.13/docs/reference/presets.md) /
[`docs/reference/extensions.md`@v0.8.13](https://github.com/github/spec-kit/blob/v0.8.13/docs/reference/extensions.md)
は、preset / extension が **独立作者により保守され、Spec Kit maintainers が
review / audit / endorse / support しない** ため、利用者側で source review が
必要であることを明記している。

LOOP が将来 preset / extension を採用する場合の guard:

```markdown
Preset / extension adoption guard:
- pin source URL to immutable tag or commit SHA (never use mutable catalog URL
  in main workflow)
- review source before install (read manifest + commands + hooks + scripts;
  reject if any path writes outside `.specify/`, `.claude/skills/speckit-*`,
  or `docs/`)
- record generated command snapshot diff in a dedicated reviewed PR (manifest
  hash drift must be auditable)
- prohibit network install during implementation loop unless explicitly allowed
  by the contract-review snapshot
- maintain a SBOM-style record of installed preset / extension version, source
  ref, install_method, and reviewer for each main-branch adoption
```

これらは ADR 0002 の reviewed snapshot 原則と整合する。本書は実装フックを
追加せず、policy 文として §6.1 ADR 追補骨子の「条件付き採用 (preset)」
セクションに引用される運用を想定する。
