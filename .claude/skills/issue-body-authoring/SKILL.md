---
name: issue-body-authoring
description: Issue 本文の生成・更新を専門とする共通参照ガイドライン。create-issue スキルと issue-author SubAgent が参照する VC 作成ガイダンス・ISSUE_AUTHOR_COVERAGE_V1 出力契約の定義。
---

# Issue Body Authoring

Issue 本文の生成・更新に関する共通参照ガイドライン。`create-issue` スキル（新規起票）と `issue-author` SubAgent（既存 Issue 更新）が共通参照する VC 作成ガイドラインと出力契約を提供する。

`ISSUE_AUTHOR_COVERAGE_V1`、follow-up 起票時の orchestrator input、`proposal_only` boundary は `.agents/skills/shared-agent-skills-governance/references/follow-up-issue-contract.md` を正本とする。本 skill では VC 作成と本文品質ガイドに責務を絞る。

## Machine-Readable Contract Block Guidance

Issue 本文に `## Machine-Readable Contract` がある場合、`issue-author` と caller は以下を守る。

- block 全体を削除・移動・ prose 化しない。変更が必要なときは YAML key の値だけを更新する。
- issue kind ごとの required key を維持する。
  - parent: `contract_schema_version`, `issue_kind`, `goal_ref`, `change_kind`, `parent_mode`, `closure_mode`
  - implementation/research: `contract_schema_version`, `issue_kind`, `parent_issue`, `goal_ref`, `change_kind`
  - human-confirm: `contract_schema_version`, `issue_kind`, `parent_issue`, `goal_ref`, `decision_type`
- `change_kind` と `decision_type` は routing/decision の最小 metadata として block に置く。
- parent issue では `parent_mode` を `delivery-rollup` / `quality-gate` / `routing-map` / `decision-log` から選ぶ。
- `closure_mode` は `child-complete` / `measurement-ready` / `quality-validated` / `routing-complete` / `decision-recorded` の closed enum を使い、placeholder のまま確定しない。
- `parent_mode` と `closure_mode` の互換は `delivery-rollup -> child-complete`、`quality-gate -> measurement-ready | quality-validated`、`routing-map -> routing-complete`、`decision-log -> decision-recorded` に固定する。
- `<required: ...>` placeholder や enum 外値は missing と同様に invalid とする。
- `## Required Skills` と `## Rules` は注釈や人間向け説明を伴うため prose section を正本に維持し、block に二重化しない。
- block の値を更新した場合は、対応する `## Parent Issue` / `## Goal` / `## Outcome` / `## In Scope` の prose と矛盾していないかを同じ編集で確認する。

## Parent Issue Authoring Guidance

- parent issue を作成・更新するときは、block の `parent_mode` と `closure_mode` だけで close 契約を済ませず、本文にも `## Quality Decision Record` と `## Parent Closure Rule` を置く。
- `delivery-rollup` は child 実装の rollup 完了を close 条件にする parent 向け、`quality-gate` は child 完了と quality decision を分離したい parent 向け、`routing-map` は canonical destination の地図を維持する parent 向け、`decision-log` は意思決定記録と次アクション固定を主目的とする parent 向けに使う。
- `closure_mode` の key は `child-complete` / `measurement-ready` / `quality-validated` / `routing-complete` / `decision-recorded` で統一し、本文の自然言語表現とは混ぜない。
- `quality-gate` parent では `#2027` を precedent とし、少なくとも `measurement-ready / quality-unvalidated` と `quality-validated` を区別できる `Status` を `## Quality Decision Record` に残す。
- `## Parent Closure Rule` では、「child issue がすべて close しても親を close しない条件」を明示できるようにする。`quality-gate` parent は child close 数ではなく Quality Decision Record の確定を close 判定に使う。
- `quality-gate` parent では `closure_mode` と `Quality Decision Record.Status` を同一編集で更新する。`closure_mode: measurement-ready` は `Status: measurement-ready / quality-unvalidated`、`closure_mode: quality-validated` は verdict と `Decision Date` / evidence を伴う QDR を前提とする。不一致は invalid で close 不可とする。
- `quality-gate + child-complete` のような互換しない組み合わせは invalid とし、body だけで close-ready と解釈できないようにする。
- `#446` の runtime guard が未実装な間は、この contract は self-enforcing ではない。quality-gate parent を body 更新だけで auto-close-ready と扱わない。
- `routing-map` / `decision-log` parent でも、close 条件を本文に prose で残し、body だけ読めば agent が close 契約を再解釈できるようにする。

## Required Skills / Rules Authoring Guidance

Issue 本文の `## Required Skills` は **runtime dependency のみ**を列挙する。`issue-author` と caller は以下を守る。

- 暗黙ワークフロースキル（`issue-contract-review` / `implement-issue` / `pr-review-judge` など、実装者向け参照スキル）は `## Required Skills` に書かない。
- rule file や rule slug（例: `.agents/rules/wsl-dev-environment.md`, `wsl-dev-environment`, `git-policy`）は `## Required Skills` ではなく `## Rules` に書く。
- spec / doc / path reference（例: `.kiro/specs/...`, `design.md`, `requirements.md`, repo 内ファイルパス）は skill ではない。`## Background` / `## In Scope` / `## Rules` の適切な section に書き、`## Required Skills` に入れない。
- current skill inventory にある canonical skill 名は、その表記をそのまま許容する。これには bare の system skill（例: `openai-docs`, `skill-creator`）と namespaced plugin skill（例: `github:gh-fix-ci`）の両方を含む。
- repo-local skill 名を書く場合は `.agents/skills/<skill-name>/SKILL.md` が実在する名前だけを使う。current skill inventory にも repo-local path にも存在しない名前は fail-close で修正し、近い rule / doc / path reference へ丸めない。
- runtime dependency がない場合は `- なし（runtime dependency なし）` と明記するか、section 全体を省略する。

## ワークフロー不具合検出時の修正方針起案ガイダンス（決定論的修正優先）

**背景**: follow-up Issue 起票時に、prompt 注記による workaround が即時起案されるバイアスが観察されている。実際には根本原因（スクリプト・コード・設定）の決定論的修正で解決可能な場合が多い。本ガイダンスは create-issue / post-merge-cleanup が follow-up Issue の Outcome 起案時に従うべき方針を定義する。

### 修正方針起案の 3 ステップ（決定論的 → workaround の順序で検討）

**ステップ 1: 根本原因の特定**
- ワークフロー不具合（CI drift / SubAgent 動作差異 / スクリプト同期失敗など）を検出したら、まず根本原因がどこにあるかを特定する
- 「どこ（スクリプト・コード・設定ファイル・運用ルール）の何が、どのように動作を変えたのか」を明記する
- 例: Issue #1859 では「bootstrap_recipe.py が環境を正しく整定していない」（スクリプトが根本原因）と特定

**ステップ 2: 決定論的修正の検討**
- 根本原因が判明したら、スクリプト・設定・コード側の決定論的修正で解決可能か検討する
- 「スクリプト修正 / config 追加 / 環境変数設定」など、再現性・テスト可能な修正案を提示する
- 決定論的修正が実現可能な場合は、それを Outcome の第一候補として Issue 本文に記載する
- 例: Issue #1859 では「WSL2 の PowerShell 経由実行を避け、bash 経由の直接実行に修正する」が決定論的修正案

**ステップ 3: workaround との明示比較**
- 決定論的修正が困難・過剰コスト・スコープ外判定の場合のみ workaround を採用する
- 「決定論的修正は検討したが、以下の理由で workaround を採用」と明示して Issue 本文に記載する
- prompt 注記による SubAgent 委譲・運用ルール追加は、決定論的修正より後の次点オプション（anti-pattern: workaround の即時起案）とする

### Anti-pattern: prompt 注記による workaround の即時起案

**避けるべき例**:
- ワークフロー不具合を検出した際、根本原因調査・決定論的修正の検討を省きがちになり、「implementation-worker SubAgent の prompt に注記を追加する」「create-issue の VC に特別条件を追記する」といった prompt-based workaround を即座に起案してしまう
- このアプローチは以下の課題を招く:
  1. **再利用不可**: workaround が prompt 注記に埋もれ、同類の不具合が再発した際に対応ルールが組織的に継承されない
  2. **コンテキスト肥大化**: SubAgent prompt に workaround が蓄積し、認知負荷が上昇して本来のタスクロジックが埋没する
  3. **テスト不可**: prompt 注記は自動テストで検証不能であり、人間レビュアーが都度チェックする必要がある

### 適用範囲

本ガイダンスは **ワークフロー不具合を起票する follow-up Issue**（post-merge-cleanup / issue-refinement-loop で自動抽出されるもの）に適用される。単純なバグ fix や軽微な改善 Issue では、煩雑さを避けるため本ステップ 3 の明示比較を省略してもよい（但し決定論的修正優先の原則は変わらない）。

## VC 作成ガイダンス

### grep vs AST ベースの選択基準

AC に「特定の関数内」で何かを確認する VC を書く場合は、`grep`（ファイル全体対象）ではなく Python AST ベースの関数スコープ限定パターンを推奨する。

判断基準:
- `grep` を使うべき場合: ファイル全体・モジュール全体で記述の有無を確認する場合
- AST ベースを使うべき場合: 特定の関数内での依存・記述の確認が必要な場合

AST パターンの必須 3 要素:
1. `found = False` フラグ（関数が存在しない場合の検出）
2. コメント行除外（`not l.lstrip().startswith('#')`）
3. docstring 除外（`'\"\"\"' not in l and \"'''\" not in l`）

```bash
python3 -c "
import ast, sys, re
src = open('<file>').read()
tree = ast.parse(src)
found = False
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == '<target_func>':
        found = True
        body_src = ast.get_source_segment(src, node) or ''
        hits = [l for l in body_src.splitlines()
                if re.search(r'<pattern>', l)
                and not l.lstrip().startswith('#')
                and '\"\"\"' not in l and \"'''\" not in l]
        if hits:
            print('FAIL:', hits); sys.exit(1)
        else:
            print('OK: <condition>')
if not found:
    print('FAIL: <func> not found'); sys.exit(1)
"
```

### 削除確認パターン

AC に「旧記述が削除されていること」を含む VC は以下のパターンを使う（grep 成功 = 残存 = FAIL）：

```bash
grep -q "削除対象の記述" <file> && echo "FAIL: 旧記述が残存" || echo "PASS: 旧記述削除済み"
```

### AC/VC 番号一致制約

Verification Commands 内の `# AC<N>` コメント番号は、Acceptance Criteria の AC 番号と必ず一致させること。

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

VC に `rg` が含まれる場合、`rg "foo\|bar"` の `\|` は `|` に修正すること（rg の OR 演算子）。

### test-runner 向け決定論的 VC と PR レビュアー向け意味的評価 AC の分離

VC はすべて **決定論的（deterministic）** な形式で作成すること。意味的評価（セマンティック）は PR レビュアーの責務であり、test-runner が実行可能な VC 内で行わせない。

**決定論的判定（test-runner が実行可能）**:
- `grep` / `rg` の exit code（パターンが存在するか否か）
- `diff` の exit code（ファイルが一致するか否か）
- `pytest` / `just check` の exit code（全テスト合格か否か）
- ファイル存在確認（`test -f` / `test -d`）
- ファイルサイズ・行数の数値比較

**意味的評価（PR レビュアーが判定）**:
- コード品質の正当性（「このコードは計画通り正しいコードか」等）
- 算出値の妥当性（「この数値は期待値として適切か」等）
- ドメイン固有の正当性（「この業務ロジックは正しいか」等）

**例：AC が「OCR 出力が改善されていること」の場合**:
- 誤り（意味的評価を test-runner に要求）: `bash scripts/live-verify.sh ... | grep "OCR 精度" | grep -v "前："` （grep hit の有無で「改善されている」を判定しようとしている）
- 正解（決定論的）: `bash scripts/live-verify.sh ... && echo "PASS: live-verify 実行成功" || echo "FAIL: live-verify 実行失敗"` （VC は実行結果の成否のみを判定）
- 意味的評価は PR レビュアーが「実行結果の出力値を目視し、期待される改善が実現されているか」を判定する

## Anchor Verification Preflight

Issue 本文で「既存ファイルの行番号・セクション見出し・関数名」を anchor として主張する場合は、起票前に以下のプリフライトを実施する。

### フロー

(a) Issue 本文中で「既存ファイル + L番号」「既存セクション見出し」「既存関数名」を anchor として主張していないかを確認する。

(b) anchor が存在する場合は、起票前に以下で hit 件数を確認する:
```bash
# anchor 文字列を git grep または rg で検索する
git grep -n "<anchor文字列>" <対象ファイルパス>
# または
rg -n "<anchor文字列>" <対象ファイルパス>
```

(c) 0 hit の場合は「該当箇所は存在しないため、更新ではなく新規追加」と Issue 本文に明記するか、anchor 主張を修正する。

### 失敗例（PR #2163 anchor 検証）

PR #2163 では `design.md` の L1187-1197 に「フッター」セクションが存在すると主張した。しかし実際に `git grep -n "フッター" .kiro/specs/kindle-content-ingestion/design.md` を実行すると 0 hit だったため、「既存セクションの更新」ではなく「新規追加」と正しく認識し直した。anchor の事前検証なしに「L番号」や「見出し名」を Issue 本文に書くと、後続実装者が存在しない行を参照しようとして混乱する。

## doc-lint baseline 記法サンプル

Issue 本文で doc-lint baseline を言及する場合は、以下の正本ファイル名を使う。

**正本ファイル名: `.doc-lint.baseline.json`**（fingerprint 抑制ファイル）

`inventory.baseline.json` は inventory diff 専用であり、fingerprint 抑制の正本ではない（PR #2208 / Issue #2142 の混同事例）。

Issue 本文サンプル：

```markdown
## In Scope
- `.kiro/specs/foo/.doc-lint.baseline.json` の fingerprint を更新する
  - `PYTHONPATH=src uv run python3 -m doc_lint.cli check --scope spec --feature foo --update-baseline`

## Verification Commands
# AC1: baseline ファイルの更新確認
test -f .kiro/specs/foo/.doc-lint.baseline.json && echo "PASS" || echo "FAIL"
grep -c '"fingerprint"' .kiro/specs/foo/.doc-lint.baseline.json
```

## Blocker 検出フロー（Issue 起票前）

Issue を起票する前に、以下のフローで blocker を検出し、必要なら `--blocked-by` を指定して起票する。

### ステップ 1: 関連 OPEN Issue の列挙

```bash
# 関連語でキーワード検索する（2〜3 語を選択）
gh issue list --search "<関連語>" --state open --json number,title,url
```

### ステップ 2: 前提条件かどうかを判定する

各候補について以下の基準で「本 Issue の前提条件か」を判定する:

1. 対象機能の実装 PR が未マージ（依存 API / ライブラリの提供元が未完了）
2. 本 Issue の実装に必要な spec / 設計書が未確定
3. 外部サービス・API が未公開で本番利用不可
4. 同一ファイルへの変更が競合する可能性がある open PR が存在する
5. 本 Issue の AC が別 Issue の完了を明示的に条件としている

上記いずれかに該当すれば、その Issue を blocker として登録する。

### ステップ 3: blocked-by として登録する場合の連携手順

`create_issue_txn.py --blocked-by` で blocker を指定して起票する:

```bash
python scripts/github_ops/create_issue_txn.py \
  --repo <owner>/<repo> \
  --title "実装: <タイトル>" \
  --blocked-by <blocker_issue_number>
```

詳細な手順・複数指定・検証コマンドは `.agents/skills/create-issue/SKILL.md` の「Blocker / Blocked-by 設定手順」セクションを参照する。

## Related

- skill: `.agents/skills/create-issue/SKILL.md` — 本ガイドラインを参照（`## doc-lint baseline 取り扱い` セクションも参照）
- agent: `.claude/agents/issue-author.md` — 本ガイドラインを参照
- skill: `.agents/skills/issue-refinement-loop/SKILL.md` — ステップ 4 で issue-author を委譲
