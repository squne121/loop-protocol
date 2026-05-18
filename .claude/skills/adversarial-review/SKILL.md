---
name: adversarial-review
description: Run a read-only adversarial software review that challenges the chosen implementation and design. Use when the user explicitly wants a skeptical challenge review, pressure testing, or focused scrutiny on risk areas such as auth, data loss, rollback, race conditions, retries, migrations, or reliability.
---

# Adversarial Review

read-only の敵対的レビューのみに使うスキル。
ファイル編集・パッチ適用・所見の自動修正は一切行わない。

## 責務境界
このスキルの責務: 信頼性・セキュリティリスクのレビュー（race condition / rollback / auth / observability 等）+ project convention 適合観点（architecture-fit）
pr-reviewer の結果を参照しないこと（相互参照禁止）。
AC 充足度の判定（AC が満たされているかどうか）は本 skill の責務外（pr-review-judge の責務）。

## Use When

以下の場合に使う:
- 現在の実装を意図的に疑いたい
- 設計上の前提・トレードオフを pressure-test したい
- race condition / rollback 危険 / retry / idempotency ギャップ / auth バグ / マイグレーションリスク / 隠れた失敗モードを探したい
- pre-ship の no-ship レビューをしたい

通常のコードレビューには使わない。

## Operating stance

懐疑をデフォルトにする。
証拠が揃うまで、変更は微妙・高コスト・ユーザー可視な形で失敗しうると仮定する。
良い意図・後続作業の予定・部分的な緩和策にはクレジットを与えない。
happy path だけで動くなら、それは実質的な弱点として扱う。

## Priority attack surface

以下を優先する:
- auth / permissions / tenant isolation / trust boundary
- data loss / corruption / duplication / 不可逆な状態変更
- rollback 安全性 / retry / partial failure / idempotency ギャップ
- race condition / ordering assumption / stale state / re-entrancy
- empty-state / null / timeout / degraded dependency の挙動
- version skew / schema drift / migration 危険 / compatibility regression
- 障害を隠す・復旧を妨げる observability ギャップ
- **（doc-lint / structure チェッカー PR 専用）library 実出力（parser AST）と自前 AST 仮定のズレ（`heading_open.children` 等の前提確認）**
- **（doc-lint / structure チェッカー PR 専用）generic deferred 判定が同一 node 内の別 REQ に漏れていないか（sentence 境界を考慮しているか）**
- **（doc-lint / structure チェッカー PR 専用）duplicate heading anchor の採番が既存 slug と衝突しないか（単純な出現回数ではなく未使用値探索が必要）**
- **（doc-lint / structure チェッカー PR 専用）profile 固有の strictness が緩んでいないか（`cc-sdd` vs `arc42` の分離）**

### architecture-fit（project convention 適合）

以下の観点を独立カテゴリ `[ARCH-FIT]` として必ず確認する:
- `.agents/`, `.claude/`, `.codex/`, `justfile`, `pyproject.toml` 等の**正規ディレクトリ・tooling の利用**が守られているか
- **ad-hoc directory / file の無断追加**（例: `.agents/runtime/`, `.agents/cache/`, `.agents/tmp/` 等）がないか
- AI 製品仕様外の `.yaml` / `.json` registry を skill / script が直接 parse する構造になっていないか（例: ハードコードされた hazard registry, capability registry 等）
- `just` / `pyproject.toml` / `uv` 等の**既存 project tooling で代替できる実装**が ad-hoc スクリプトとして書かれていないか
- CLAUDE.md §5「参照先」（`.agents/rules/`, `.agents/skills/`, `.kiro/steering/`, `.kiro/specs/`）以外への新規ファイル作成がないか（意図的である場合は PR 本文での説明を確認する）
- `spec-status` 観測で解決した canonical surface と PR の touched paths が一致しているか（canonical surface 以外の path に修正が広がっていないか）

**severity 基準（architecture-fit）:**
- ad-hoc dir 新設 + project tooling で代替可能な実装の重複 → `HIGH`
- AI 製品仕様非準拠の registry を直接 parse → `HIGH`
- 正規ディレクトリへの配置で解決可能な構造 → `MEDIUM`
- CLAUDE.md §5 参照先外への新規ファイル（説明なし） → `MEDIUM`

**重要**: architecture-fit 指摘は `[ARCH-FIT]` プレフィックスを `title` に付与し、信頼性リスク指摘と区別すること。AC 明示要求（Issue contract に記載された設計判断）への実装は、たとえ convention から外れていても `[ARCH-FIT]` として `MEDIUM` 以下で報告し、`CRITICAL/HIGH` に分類しない。

## Procedure

### Step 0: 入力コンテキストの準備（direct 実行・SubAgent 委任ともに必須）

adversarial-reviewer SubAgent に委任する場合、main conversation は以下を事前に取得してプロンプトに含めて渡すこと:

1. Review 対象の差分:
   ```bash
   gh pr diff <pr_number>         # PR レビュー時
   git diff <base>...<head>       # ブランチ・作業ツリーレビュー時
   ```

2. 変更ファイルリスト:
   ```bash
   gh pr diff --name-only <pr_number>
   git diff --name-only <base>...<head>
   ```

3. PR 本文（PR レビュー時）:
   ```bash
   gh pr view <pr_number> --json body -q .body
   ```

4. Issue contract（Issue と紐づいている場合）:
   ```bash
   gh issue view <issue_number>
   ```

これらの情報がない場合、adversarial-reviewer SubAgent は `INSUFFICIENT_CONTEXT` を報告して停止する。

### Step 1: Review 対象を解決する

- explicit base ref => `<base>...HEAD`
- explicit working tree => staged + unstaged + untracked
- explicit branch review（base なし）=> default branch との差分
- auto: dirty tree => working tree / clean tree => current branch vs default branch

### Step 2: 対象 diff・ファイルを read-only コマンドで確認する

```bash
git diff <base>...<head>
git show <commit>:<file>
git log --oneline <base>...<head>
```

### Step 3: Priority attack surface の各観点で不変条件・guard・失敗パスを検証する

- bad input / retry / concurrency / partially completed operation がコード内をどう伝播するか追跡する
- ユーザーが focus area を指定した場合はそこに重点を置くが、他の material issue も報告する
- `.agents/skills/` から `.claude/skills/` への同期差分は generated mirror として扱う。`bash scripts/sync-agent-skills.sh` / `just sync-skills` による投影結果は、source 側の変更と対応しているかを確認するために見るが、投影ファイルそのものを独立した scope violation として扱わない
- ただし、`.claude/skills/` に source 由来でない変更、同期漏れ、または `.agents/skills/` と対応しない差分がある場合は、通常どおり重大な不整合として扱う
- **baseline failure の分離（impl-review-loop との連携）**: impl-review-loop の Step 3 として呼び出された場合、各 finding について「baseline failure（main ブランチ既存問題）か今回差分 blocker か」を明示すること。判断基準:
  - baseline failure: diff に含まれないファイル・行に由来する問題、または PR 作成前から main ブランチに存在していた既知問題
  - 今回差分 blocker: diff 内の変更が直接引き起こしている問題（今回の PR で導入・破壊されたもの）
  - 判別が困難な場合は「不明（要確認）」と明記し、MEDIUM 以下の severity として報告する

### tasks.md タスク状態の事実確認（指摘生成前必須）

tasks.md のタスク番号や状態に言及する指摘を生成する前に、対象の行を必ず確認し以下の3状態を判定すること:

- `[ ]`（active/未着手）: 現在の作業対象。指摘対象になりうる。
- `[x]`（completed/完了）: 既に完了したタスク。「廃止」「未対応」として誤報告しないこと。
- `~~<タスク内容>~~`（deprecated/廃止）: 廃止されたタスク。廃止されている事実を正確に報告する。

**重要**: `[x]` の完了タスクを `~~...~~` の廃止タスクと混同しないこと。「Issue 参照がない廃止タスク」などの指摘を生成する前に、当該行が `[x]` か `~~...~~` かを確認すること。「完了」と「廃止」は異なる意味であり、どちらが正しい状態かを見極めてから指摘を生成する。

### Step 4: Material findings のみ構造化して出力する

- style / naming / cleanup は出力しない
- 各 finding は「何が問題か」「なぜ脆弱か」「影響範囲」「具体的な改善策」の4点を答えること
- 弱い指摘を大量に出すより、根拠の強い重大指摘を優先する

## Output Contract

structured output は `.agents/skills/adversarial-review/references/review-output-schema.json` で定義する。

- `verdict`: `approve` または `needs-attention`
- `summary`: ship / no-ship の簡潔な判定文（中立的な要約ではなく判定として書く）
- `findings`: 各 finding には `severity` / `title` / `body` / `file` / `line_start` / `line_end` / `confidence` / `recommendation` のみを含む
  - `finding_id`: 所見の安定ID。adversarial-reviewer が出力する場合はプレースホルダー（`null` または空文字列）を出力。オーケストレータが後補。
  - `scope_classification`: 所見の scope 分類。adversarial-reviewer が出力する場合はプレースホルダー（`null`）を出力。オーケストレータが `in_scope` / `out_of_scope` / `wip_downgraded` / `contradiction` のいずれかを付与。
- `out_of_scope_followups`: `findings` と独立した top-level 配列。scope 外の見落としや再検討事項をまとめる。必要なければ空配列。
- `next_steps`: 対応アクション

`approve` は、利用可能な証拠から実質的な adversarial finding を一切支持できない場合のみ使う。

### Finding 出力フォーマット（必須）

各 finding には以下の要素を必ず含めること：

- **severity**: CRITICAL / HIGH / MEDIUM / LOW
- **根拠**: `ファイルパス:行番号` または Issue 本文の引用テキスト（**必須**）
  - 例: 根拠: `.agents/skills/adversarial-review/SKILL.md:45` — "ADV_VERDICT コメント投稿は必須"
  - 根拠が取得できない場合（Issue 本文から対応する記述が見つからない、またはファイル行番号の確認ができない場合）は、finding 末尾に `[仮説: 調査が必要]` タグを明示すること
- **修正提案**: 具体的な修正方法（オプションだが推奨）

#### 根拠なし finding の扱い

根拠（ファイルパス・行番号・Issue 本文引用）を確認できない finding には `[仮説: 調査が必要]` タグを付けること。
`[仮説: 調査が必要]` タグ付き finding は CRITICAL/HIGH の厳密性を損なうため、issue-refinement-loop オーケストレーター側で MEDIUM 以下として扱われる（収束ブロック対象外）。

## Guardrails

- review only / no fixes / no patch application / no file edits
- ファイル・行番号・コードパス・実行時挙動を捏造しない
- 推論を含む場合は明示し、confidence を正直に設定する
- 弱い指摘より根拠の強い重大指摘を優先する
- material findings のみ報告する（style / naming / cleanup は含めない）

## Stop Conditions

- review 対象が特定できない場合は、対象を明示するよう要求する
- Step 0 の情報が欠けている場合は `INSUFFICIENT_CONTEXT` を返す

## Validation Commands

```bash
# Skill 同期確認
just sync-skills-check
ls .claude/skills/adversarial-review/SKILL.md
```

## Refinement Phase Context（refinement フェーズ向け特別ルール）

呼び出し元が `phase: refinement` をプロンプトに含めて委譲している場合、以下のルールを **Operating stance・Priority attack surface より優先して** 適用すること:

- **review 対象の制限**: 実装コード・PR diff ではなく、Issue 本文（Outcome/AC/VC/Allowed Paths/Stop Conditions）の品質のみを評価対象とする
- **baseline FAIL 誤検知の禁止**: 対象ファイル（spec/skill/design 等）を読んで「実装前の記述が残っている」「AC に対応する変更がまだない」ことを CRITICAL / HIGH finding として報告してはならない
  - 例: 「requirements.md に廃止マークがない」「design.md に旧仕様が残存」「tasks.md にチェックマークがない」は、refinement フェーズでは **baseline FAIL（正常状態）** であり Issue 品質問題ではない
  - 「実ファイルを読んで実装未完了を観察することは正常状態である」というのが refinement フェーズの前提
- **アンチパターン（絶対禁止）**: 実ファイルを読んで実装未完了を観察し、それを根拠に「CRITICAL: requirements.md に変更が反映されていない」等と指摘すること

> 背景: Issue #1235 / #1227 において adversarial-reviewer が refinement フェーズで実装前 baseline を CRITICAL/HIGH と誤報告し、不要な fact-check ラウンドが発生した。ガードは呼び出し元に分散させるのではなくスキル本体に定義する（DRY 原則）。

## Related

- skill: `.agents/skills/pr-review-judge/SKILL.md`
- agent: `.claude/agents/security-reviewer.md`
- agent: `.claude/agents/adversarial-reviewer.md`
- reference: `.agents/skills/adversarial-review/references/review-output-schema.json`
