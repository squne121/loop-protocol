---
name: issue-body-authoring
description: Issue 本文の生成・更新を専門とする共通参照ガイドライン。create-issue スキルと issue-author SubAgent が参照する VC（Verification Commands）作成ガイダンス・本文品質ガイドの定義。
---

# Issue Body Authoring

`create-issue`（新規起票）と `issue-author` SubAgent（既存 Issue 更新）が共通参照するガイドライン。本 skill は VC 作成と本文品質に責務を絞る。

follow-up Issue 起票時の入力契約は `create-issue` の SKILL.md 内に最小定義する（共通契約 governance package は持たない）。

## Machine-Readable Contract Block Guidance

Issue 本文に `## Machine-Readable Contract` がある場合、`issue-author` と caller は以下を守る。

- block 全体を削除・移動・prose 化しない。変更が必要なときは YAML key の値だけを更新する
- issue kind ごとの required key を維持する:
  - `parent`: `contract_schema_version`, `issue_kind`, `goal_ref`, `change_kind`, `parent_mode`, `closure_mode`
  - `implementation` / `research`: `contract_schema_version`, `issue_kind`, `parent_issue`, `goal_ref`, `change_kind`
- `parent_mode` は `delivery-rollup | quality-gate | routing-map | decision-log` の closed enum
- `closure_mode` は `child-complete | measurement-ready | quality-validated | routing-complete | decision-recorded` の closed enum
- `parent_mode` と `closure_mode` の互換: `delivery-rollup → child-complete` / `quality-gate → measurement-ready | quality-validated` / `routing-map → routing-complete` / `decision-log → decision-recorded`
- `<required: ...>` placeholder や enum 外値は missing と同様に invalid
- `## Required Skills` / `## Rules` は prose section を正本に維持し、block に二重化しない
- block 更新時は対応する prose（`## Outcome` / `## In Scope` / `## Parent Issue` 等）と矛盾していないか同編集で確認する

## Parent Issue Authoring Guidance

- parent issue は `parent_mode` と `closure_mode` だけで close 契約を済ませず、`## Quality Decision Record` と `## Parent Closure Rule` を本文に置く
- `quality-gate` parent は child close 数ではなく Quality Decision Record の確定を close 判定に使う
- `closure_mode` と `Quality Decision Record.Status` は同一編集で更新する。不一致は invalid で close 不可
- `quality-gate + child-complete` のような互換しない組み合わせは invalid
- `routing-map` / `decision-log` parent でも close 条件を本文に prose で残し、本文だけで agent が close 契約を再解釈できるようにする

## Required Skills / Rules Authoring Guidance

Issue 本文の `## Required Skills` は **ドメイン知識スキル** のみを列挙する（例: TypeScript / ECS / Canvas / Vitest BDD 等）。

- ワークフロー skill（`issue-contract-review` / `implement-issue` / `pr-review-judge` / `ssot-discovery` 等）はここに書かない。skill 間で関係性が定義されているため
- rule 参照は per-directory `CLAUDE.md`（Claude Code 自動ロード）と `ssot-discovery` skill で行うため、`## Rules` への列挙は最小限に
- spec / doc / path reference（`docs/adr/...`, `docs/product/requirements.md` 等）は skill ではない。`## Background` / `## In Scope` の適切な section に書く
- runtime dependency がない場合は `- なし` と明記するか、section 全体を省略する

## ワークフロー不具合検出時の修正方針起案ガイダンス（決定論的修正優先）

follow-up Issue 起票時、prompt 注記による workaround を即時起案するバイアスを避けるため、以下の 3 ステップで修正方針を検討する。

**ステップ 1: 根本原因の特定**
「どこ（スクリプト・コード・設定ファイル・運用ルール）の何が、どのように動作を変えたのか」を明記する。

**ステップ 2: 決定論的修正の検討**
スクリプト・設定・コード側の決定論的修正で解決可能か検討する。再現性・テスト可能な修正案を Outcome の第一候補として記載する。

**ステップ 3: workaround との明示比較**
決定論的修正が困難・過剰コスト・スコープ外の場合のみ workaround を採用する。「決定論的修正は検討したが、以下の理由で workaround を採用」と明示する。
prompt 注記による SubAgent 委譲・運用ルール追加は次点オプション（anti-pattern: workaround の即時起案）。

### 適用範囲

ワークフロー不具合を起票する follow-up Issue（post-merge-cleanup / issue-refinement-loop から自動抽出されるもの）に適用する。単純なバグ fix や軽微な改善 Issue では本ステップ 3 の明示比較を省略してよい（決定論的修正優先の原則は維持）。

## VC 作成ガイダンス

### 決定論的 VC と意味的評価 AC の分離

VC はすべて **決定論的（deterministic）** な形式で作成すること。意味的評価は PR レビュアーの責務であり、test-runner が実行可能な VC 内で行わせない。

**決定論的判定（test-runner が実行可能）**:
- `grep` / `rg` の exit code（パターンの存在）
- `diff` の exit code（ファイル一致）
- `pnpm typecheck && pnpm lint && pnpm test && pnpm build` の exit code（全テスト合格）
- ファイル存在確認（`test -f` / `test -d`）
- ファイルサイズ・行数の数値比較

**意味的評価（PR レビュアー判定）**:
- コード品質の正当性
- 算出値の妥当性
- ドメイン固有の正当性

### TypeScript 関数スコープ限定 VC

AC に「特定の関数内」で何かを確認する VC を書く場合、`grep`（ファイル全体）ではなく **TypeScript の AST または関数境界の正規表現** で関数スコープに限定する。

Vitest など実行系で確かめられる場合は `pnpm test path/to/specific-test` を優先するのが最も決定論的。

### 削除確認パターン

AC に「旧記述が削除されていること」を含む VC:

```bash
grep -q "削除対象の記述" <file> && echo "FAIL: 旧記述が残存" || echo "PASS: 旧記述削除済み"
```

### AC/VC 番号一致制約

Verification Commands 内の `# AC<N>` コメント番号は、Acceptance Criteria の AC 番号と必ず一致させる。

例:
```
## Acceptance Criteria
- [ ] AC1: ファイルが存在する
- [ ] AC2: 特定のフィールドが含まれている

## Verification Commands
# AC1: ファイル存在確認
test -f <file> && echo "PASS: AC1" || echo "FAIL: AC1"

# AC2: フィールド確認
grep -q "<field>" <file> && echo "PASS: AC2" || echo "FAIL: AC2"
```

### rg 構文チェック

VC に `rg` が含まれる場合、`rg "foo\|bar"` の `\|` は `|` に修正する（rg の OR 演算子）。

## Anchor Verification Preflight

Issue 本文で「既存ファイルの行番号・セクション見出し・関数名」を anchor として主張する場合、起票前にプリフライトを実施する。

### フロー

(a) anchor 主張（「既存ファイル + L番号」「既存セクション見出し」「既存関数名」）の有無を確認する。

(b) anchor が存在する場合は、起票前に hit 件数を確認:
```bash
git grep -n "<anchor文字列>" <対象ファイルパス>
# または
rg -n "<anchor文字列>" <対象ファイルパス>
```

(c) 0 hit の場合は「該当箇所は存在しないため、更新ではなく新規追加」と本文に明記するか、anchor 主張を修正する。

## Blocker 検出フロー（Issue 起票前）

Issue を起票する前に、関連 OPEN Issue を列挙し、依存関係を判定する。

### ステップ 1: 関連 OPEN Issue の列挙

```bash
gh issue list --search "<関連語>" --state open --json number,title,url
```

### ステップ 2: 前提条件かどうかを判定する

以下のいずれかに該当すれば blocker:

1. 対象機能の実装 PR が未マージ（依存 API / ライブラリの提供元が未完了）
2. 本 Issue の実装に必要な spec / 設計書が未確定
3. 同一ファイルへの変更が競合する可能性がある open PR が存在する
4. 本 Issue の AC が別 Issue の完了を明示的に条件としている

### ステップ 3: blocked-by として登録する場合

`create_issue_txn.py --blocked-by` で blocker を指定して起票する:

```bash
python3 .claude/skills/create-issue/scripts/create_issue_txn.py \
  --repo <owner>/<repo> \
  --title "実装: <タイトル>" \
  --blocked-by <blocker_issue_number>
```

詳細手順は `.claude/skills/create-issue/SKILL.md` を参照。

## Related

- `.claude/skills/create-issue/SKILL.md` — 新規起票で本ガイドラインを参照
- `.claude/agents/issue-author.md` — 既存 Issue 更新で本ガイドラインを参照
- `.claude/skills/issue-refinement-loop/SKILL.md` — ステップ 4 で issue-author を委譲
- `.claude/skills/ssot-discovery/SKILL.md` — Issue 関連 SSOT の探索
