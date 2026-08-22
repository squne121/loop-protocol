---
adr_id: "0008"
title: "Deterministic Evidence vs Model-Based Semantic Judgment の役割分担"
summary_ja: "決定論的に確認できる事実は script に委譲し、意味論的な設計判断は LLM の推論に委ねるという役割分担原則を定める決定記録。issue-refinement-loop が semantic design finding を未解決のまま処理完了扱いにした構造的欠陥（#2273）を踏まえ、materiality-triggered な semantic review と advisory-by-default（個人開発向けの軽量運用）を採用する。"
status: accepted
decision_date: "2026-08-22"
confirmed_date: "2026-08-22"
related_issues:
  - "#2295"
  - "#2273"
  - "#2294"
supersedes: []
superseded_by: null
---

# ADR 0008: Deterministic Evidence vs Model-Based Semantic Judgment の役割分担

<!--
frontmatter キーの運用規約:
- adr_id: ADR 番号を 4 桁の文字列で表現（leading zero を保つため必ず quote する）
- title: frontmatter と H1 で重複させる場合は frontmatter を正本とする
- status: proposed → accepted → (superseded | deprecated) の lifecycle で管理
- decision_date: accepted に遷移した日（proposed 段階では null 可）
- confirmed_date: 別 Issue / PR 等での再確認が完了した日。未確認なら null
- related_issues: 関連 GitHub Issue / PR を配列で列挙（最低 1 件）
- supersedes / superseded_by: ADR 間の代替関係を ADR ID 配列で表現
-->

## Context（背景）

`issue-refinement-loop`（`.claude/skills/issue-refinement-loop/`）は、Issue 本文の contract
妥当性を `check_issue_contract.py`（C1〜C13）と `decide_rewrite_route.py` という deterministic
script に委譲することで、繰り返し実行される loop の token/context コストを抑制している。
`decide_rewrite_route.py` の `_REPAIRABLE_FIX_CATEGORIES = {"missing_section",
"missing_contract_key", "unknown_contract_failure"}` は「Issue 本文の構文的完全性」しか
判定対象にしておらず、schema 設計判断・AC の完全性・architecture のトレードオフといった
semantic（意味論的）な設計判断は最初から scope 外である。`SKILL.md` も Step 3（adversarial
review）を明示的に不採用として記載している（背景は #428 を参照。#428 は `impl-review-loop` と `issue-refinement-loop` の両方を対象とする
narrow な条件付き有効化案であり、Child A（#2296）は本 ADR が定める原則に沿って
issue-refinement-loop 側の scope のみを Step 2.5 として再設計する。#428 の impl-review-loop 側
residual scope は Child A の Allowed Paths に含まれず、Child A 完了後も別途 disposition
（重複整理または独立実装）が必要である）。

Issue #2273（「Claude-GPT に workflow-profile capability preflight と controlled GitHub route
を配線する」実装 Issue）の敵対的レビューにおいて、`issue-refinement-loop` の 1 周が
P0-3〜P0-6／P1-1〜P1-5 級の設計上の finding（#2223 の現行 Owner Decision との不整合、
`launch.sh` の credential 隔離アーキテクチャと GitHub write positive lane の構造的不整合など）
を検出したにもかかわらず、「機械的な文言置換では対応できないため次回 iteration や
`issue-contract-review` に委ねる」という自己申告のまま loop を正常終了させてしまった。
この時点の deterministic checker（`check_issue_contract.py` / `decide_rewrite_route.py`）は
`review_verdict` と `iteration` のみを終了判定の入力にしており、anchor comment 由来の
未解消 semantic finding の有無を終了ゲートの判定材料にしていなかった。

4 系統の独立調査（現行アーキテクチャ監査／履歴監査／`impl-review-loop` との比較監査／
Claude Code 公式仕様調査）と 3 回の独立設計レビュー（bootstrap semantic review／adversarial
review／contract-regression review）を経て、これは退行（regression）ではなく
「一度も制度化されなかった gap（never-built gap）」であると確定した（#2294 参照）。
歴史的検証（Issue/PR #293/#294/#296 等）では、script-first 化の目的は純粋な usage コスト
削減であり、semantic review を機械化・禁止する Owner Decision は存在しない。むしろ
`pr-review-judge` の AC6（`.claude/skills/pr-review-judge/SKILL.md#AC6`、
「Deterministic Processing Script の禁止事項」）は semantic finding の自動生成を
script に禁止し、verdict の最終判断は LLM（`pr-reviewer`）側に予約している。PR review 側の
`impl-review-loop`／`pr-reviewer`／`pr-review-judge`／`pr-reviewer-lite` にはこの役割分担が
既に実装されているが、Issue 側（`issue-refinement-loop`）には同等の制度が存在しなかった。

本 ADR は、この deterministic evidence（決定論的に確認できる事実）と semantic judgment
（意味論的な設計判断）の役割分担原則を、`issue-refinement-loop` に限らず repository 全体の
LLM-orchestrated workflow に適用可能な規範として明文化する。

## Considered Options（検討した選択肢）

### Option A: script 側で semantic 判定を機械化する

`check_issue_contract.py` 等の deterministic checker に、schema 設計判断・AC 完全性・
architecture trade-off を判定するルールを追加し、finding の検出から解消判定まで script が
完結させる案。

- 長所: 実行コストが最小で、判定結果が再現可能（reproducible）になる。
- 短所: schema 設計判断や architecture trade-off は本質的に有限の decision tree に還元
  できない（新しい Issue が提起する論点は事前に列挙不可能）。無理に機械化すると、
  ルールに一致しない finding を「該当なし」と誤判定する偽陰性のリスクが高く、
  `pr-review-judge` AC6 が禁じる「script による semantic finding の自動生成」と同じ
  失敗パターンを Issue 側に持ち込むことになる。

### Option B: LLM に一任し、gate を設けない（現状維持）

deterministic checker は構文的完全性のみを見続け、semantic judgment は main thread の
LLM 裁量に完全に委ねる（追跡・強制の gate を新設しない）案。

- 長所: 実装コストがゼロ。既存の柔軟性を維持できる。
- 短所: #2273 で実際に発生した「機械的に直せないので次回に委ねる」という自己申告のまま
  loop が正常終了する構造的欠陥をそのまま放置することになる。LLM 裁量に完全に依存すると、
  loop 終了ゲートが semantic finding の解消状態を一切考慮しないため、finding が
  黙示的に握りつぶされても外部から検知できない。

### Option C: deterministic gate + LLM semantic review lane を併用する（無条件・毎回実施）

facts/invariants/syntax/reproducible checks/routing mechanics/evidence
collection/transactional mutation は deterministic script に委譲し続ける一方、
AC・schema・architecture・workflow contract など決定論的に解けない領域には
明示的な LLM semantic review lane（`issue-refinement-loop` では Step 2.5 として
Child A（#2296）で追加予定）を新設する。deterministic gate は
「semantic review lane が finding を検出したか／finding が未解消のまま終了しようと
しているか」という **事実** をルーティングの入力にする（finding の意味内容そのものの
判定は行わない）。

- 長所: deterministic script は従来どおり reproducible な事実確認に専念でき、
  semantic 判断は LLM の推論能力を活かす形で残る。両者の境界が明確なため、
  `pr-review-judge` AC6 が確立した「script は verdict を生成しない」パターンを
  Issue 側にも適用でき、既存の PR review 側の設計と整合する。
- 短所: 新しい review lane の追加によりコストが増える。ただし対象は
  「決定論的に解けない領域」に限定されるため、全 Issue に一律で高コストな
  semantic review を強制するわけではない（適用条件は Child A の実装 Issue で確定する）。
  一方で Option C 自体は「semantic review を毎回無条件に実施する」ことのみを定めており、
  finding の severity 区分や missing/stale artifact 時の挙動（advisory か hard stop か）を
  規定していない。個人趣味開発の運用コストを踏まえると、この不足を放置したまま
  Option C を採用すると、軽微な finding や artifact 取得失敗が過剰に loop をブロック
  する運用に陥りやすい（Owner 敵対的レビュー、PR #2298 コメント参照）。

### Option D: 実質変更時のみ semantic review を起動し既定は advisory とする（採用）

Goal/Outcome/AC/schema/architecture/Owner Decision 等の実質変更、既存の重大 finding、
または明示要求がある場合のみ semantic review を実施する。review artifact の
missing/stale は一度だけ自動再実行し、それでも取得不能なら warning または
main-thread review へ fallback する。hard block は fresh かつ重大な未解消 finding に
限定する。個人開発では以下の二モードとする。

- `advisory` — デフォルト。ワークフローを止めない。
- `strict` — 明示指定時のみ、missing/stale も停止条件にする。

- 長所: Option C が確立した deterministic/LLM 役割分担の原則（script は semantic
  verdict を生成しない、境界は syntax/semantics ではなく形式化済みか否かで引く）は
  そのまま維持しつつ、個人趣味開発の運用実態（Owner 自身が主要な実行者であり、
  過剰な自動停止がむしろ生産性を損なう）に合わせてコストとブロッキング挙動を
  最小化できる。severity/disposition による段階的な扱い（後述 Decision 規範3）で
  「本当に重大な未解消 finding だけを止める」ことが可能になる。
- 短所: `advisory` モードでは、Owner が能動的に review 結果を確認しない限り
  finding が黙示的に見過ごされるリスクが Option C（毎回無条件実施）より高い。
  この短所は「重大 finding は severity=blocker/high で hard block する」規範3で
  部分的に緩和する設計とする。

## Decision（決定事項）

Option D（materiality-triggered semantic review + advisory-by-default）を採用する。
Option C が定めた deterministic/LLM 役割分担の原則自体（下表）は維持し、
役割分担の "適用トリガー" と "終了ゲートの厳格さ" のみを Option D の軽量方針へ
置き換える。

役割分担の原則を以下のとおり固定する。

```
Deterministic code: facts / invariants / syntax / reproducible checks /
                     routing mechanics / evidence collection /
                     transactional mutation / race guards
LLM reasoning:       semantics / architecture / requirement completeness /
                     AC 設計 / schema 設計 / trade-offs /
                     contradiction resolution / evidence interpretation /
                     fix design
Orchestrator:        context budgeting / delegation / result joining /
                     bounded iteration / authority-aware escalation
```

この原則から導かれる規範を以下に定める。

1. **deterministic script に semantic verdict を生成させない**。script は「構文的に
   完全か」「型・キー集合が契約と一致するか」「再現可能な事実」を判定するに留め、
   AC の意味的十分性や architecture trade-off の当否を判定しない。これは
   `.claude/skills/pr-review-judge/SKILL.md#AC6` が既に確立している境界を Issue 側にも
   適用するものである。
2. **semantic review は materiality-triggered とする**。以下のいずれかに該当する場合
   にのみ semantic review lane を実行する。それ以外の変更（typo 修正、文言の言い換え、
   既に形式化された contract に対する機械的な整合修正など）には semantic review を
   強制しない。
   - Goal / Outcome / AC / schema / architecture / Owner Decision に実質的な変更がある
   - 既存の未解消 finding（severity: blocker または high）がある
   - Owner またはワークフローから明示的に semantic review が要求されている
   決定論的に判定できないことを、それ自体で human escalation の理由にはしない。
   LLM が repository evidence、外部一次資料、Owner Decision、Issue/PR history から
   十分な根拠を得て合理的に決定できる場合は、LLM が判断・修正案の作成まで担当する。
   human escalation は次のような genuinely human-only なケースに限定する。
   - 複数の合理的な設計案の間で Owner の価値判断が必要な場合
   - canonical な Owner Decision 同士が矛盾している場合
   - 現在の Agent/SubAgent の authority が不足している場合
   - bounded 調査を尽くしても dispositive な evidence が得られない場合
   - Owner が明示的に人間判断を要求した場合
3. **finding は severity と disposition を持ち、loop 停止条件はこの組み合わせに限定する**。
   finding 単位に最低限、以下のフィールドを持たせる。
   ```yaml
   severity: blocker | high | medium | low
   disposition: open | fixed | accepted | deferred | not_applicable
   ```
   通常モード（`advisory`）で loop を停止する条件は、次の論理積に限定する。
   ```text
   fresh model assessment
   AND severity in {blocker, high}
   AND disposition == open
   ```
   `medium` / `low` は warning 扱いとし loop を止めない。`accepted` / `deferred` は
   終了を許可する。個人開発であることを踏まえ、`accepted` は Owner が理由を一行
   記録すれば十分とする（記録先は sidecar artifact または PR/Issue コメント）。

   ここで言う「fresh model assessment」とは、deterministic code が証明できる
   **事実**（`artifact_valid` / `input_binding_valid` / `freshness_valid` の 3 つ）とは
   区別される。`approve` / `needs_fix` に相当する判定（フィールド名は
   `model_assessment` または `review_disposition` とし、`semantic_resolution_fact` の
   ような「事実」を示唆する用語は用いない）は、現在の Issue body / diff に対して
   都度再実行された LLM のフレッシュな判断であり、決定論的な真理ではない。
   deterministic code は `model_assessment` / `review_disposition` フィールドの
   schema 妥当性・現行 body_sha256 との freshness 一致・provenance のみを検証し、
   diff の有無・キーワードの消失・anchor comment の残存有無などから解消状態を
   自ら推測・再計算してはならない（規範1の適用範囲に「finding の解消判定」も
   含まれることの明示）。同時に、単発の LLM 判定を repository-wide な最終
   authority として扱ってはならない。判定はあくまで実行時点の model assessment
   であり、次回実行や別モデル・別 reviewer による再評価で覆り得るものとして
   運用する。
4. **review artifact の missing/stale は原則 advisory とし、hard stop は strict モード
   限定にする**。semantic review artifact が missing または stale（freshness 不一致）
   の場合、まず一度だけ自動再実行を試みる。再実行後も取得不能な場合の扱いは
   モードによって分岐する。
   - `advisory`（デフォルト）: warning として記録し loop を継続する。必要に応じて
     main-thread review へ fallback する。
   - `strict`（明示指定時のみ）: hard stop（`ACTION_HUMAN_ESCALATION` 相当）とする。
   通常運用（`advisory`）では missing/stale の存在だけで loop を停止しない。
5. **semantic review lane の追加は既存の固定 wire フォーマットを破壊しない**。
   `ISSUE_REVIEW_RESULT_COMPACT_V2`（固定行数の wire）や
   `LOOP_REWRITE_ROUTER_STATE_V1`（`additionalProperties: false`）に直接フィールドを
   追加せず、新規の sidecar artifact として分離する（具体的なフィールド設計は
   Child A（#2296）の scope）。

## 関連制約との関係（#1854 / #428 / pr-review-judge AC6）

- **#1854 との関係**: #1854 は snapshot や preflight artifact の
  missing/stale/invalid/runtime error で implementation や PR publication を止めない、
  という既存方針を定めている。本 ADR は Decision 規範4で semantic sidecar artifact の
  missing/stale を advisory-by-default（Option D）にすることで、この既存方針と整合
  させる。artifact 取得失敗という運用上の摩擦要因が、個人開発のワークフロー全体を
  止める理由にならないという扱いを、semantic review lane にも一貫して適用する。
- **Issue #428 との関係**: #428 は security/auth/release 変更、canonical source との
  矛盾、high-confidence overlap、人間明示要求という限定条件でのみ adversarial review
  を起動する案である。この trigger 条件は、本 ADR の Decision 規範2が定める
  materiality-triggered 条件（Goal/Outcome/AC/schema/architecture/Owner Decision の
  実質変更、既存の重大 finding、明示要求）と概ね同種であり、Child A（#2296）実装時に
  両者の統合を検討する。
- **`pr-review-judge` AC6 との関係**: AC6 は script が semantic finding を生成することを
  禁じるが、「deterministic gate の boolean を集約して APPROVE/REQUEST_CHANGES を出す
  こと」自体は明示的に許容している。本 ADR が定める境界線は「syntax 対 semantics」
  ではなく、次の 3 区分で置き換える。
  - 既に形式化された要件は deterministic test で検証してよい
  - 未形式化・曖昧・新規な設計判断は LLM が担当する
  - 繰り返し発生し安定して形式化できる finding は、費用対効果が合えば
    deterministic test へ昇格する

## Consequences（帰結）

### Child A（semantic design review lane 実装、#2296、本 ADR に blocked_by）への引き継ぎ事項

- `issue-refinement-loop` に「Step 2.5」として semantic design review lane を追加する。
  過去に不採用となった「Step 3」（#428 の adversarial review 案）という番号は再利用しない。
- semantic review lane は Decision 規範2の materiality-triggered 条件に基づいて
  起動可否を判定する（無条件・毎回実施ではない）。
- semantic review 用の新規 SubAgent は Sonnet 5 に `effort: high` を指定する
  （Claude Code 公式仕様 https://code.claude.com/docs/en/model-config#adjust-effort-level
  で対象 model への effort 指定が正式サポート済みと確認済み）。
- 新規 sidecar artifact（`semantic_review_result_v1` 等）は既存の固定 wire フォーマット
  （`ISSUE_REVIEW_RESULT_COMPACT_V2` / `LOOP_REWRITE_ROUTER_STATE_V1`）を変更せずに追加し、
  finding には Decision 規範3の `severity` / `disposition` フィールドを持たせる。
- loop 終了ゲート（`decide_next_loop_action.py` 等）に、Decision 規範3の停止条件
  （fresh model assessment AND severity in {blocker, high} AND disposition == open）を
  終了判定の入力として追加する。`medium`/`low` は warning、`accepted`/`deferred` は
  終了を許可する。
- artifact の missing/stale ハンドリングは Decision 規範4（一度だけ自動再実行 →
  `advisory` は warning 継続、`strict` のみ hard stop）に従って実装する。
  `advisory` をデフォルトモードとする。
- #428（Step 3 adversarial review の例外ゲート化、narrow scope）との重複要否を
  Child A 実装時に判断し、重複する場合は #428 を close するか、#428 の残存部分を
  Child A に統合するかを決める（#428 の trigger 条件は本 ADR の materiality-triggered
  条件と概ね同種であるため、統合の実務的コストは低いと見込む）。

### Child B（docs 明文化、#2297、Child A に blocked_by）への引き継ぎ事項

- `impl-review-loop`／`pr-reviewer`／`pr-review-judge`／`pr-reviewer-lite` に既に実装
  されている deterministic evidence／semantic judgment の分離パターンを、本 ADR が
  定める一般原則の具体例として `docs/dev/agent-skill-boundaries.md` 等へ明文化する。
- `issue-refinement-loop` 以外の skill（`review-issue` 等）への本原則の展開は
  Child B の scope 外であり、必要ならさらなる follow-up Issue として別途検討する
  （#2294 の Remaining Parent Gaps を参照）。

### 既存 ADR との重複確認結果

既存 7 件の ADR（`docs/adr/0001-architecture-baseline.md` 〜
`docs/adr/0007-agent-retrospective-boundaries.md`）および
`docs/adr/agent-skill-surface-sharing.md` を精査した結果、本 ADR の Decision と
直接重複・矛盾する既存 ADR は **存在しない**。本 ADR は新規の決定記録として追加する
（`supersedes: []`）。

関連性はあるが scope が異なるものとして、`docs/adr/0007-agent-retrospective-boundaries.md`
が定義する「deterministic extraction」（untrusted な Issue/PR 本文から構造化データを
抽出し、provenance/taint を付与した上で isolated observation context を経由させる
セキュリティ境界）を挙げておく。0007 の deterministic extraction は
「untrusted content をどう安全に LLM プロンプトへ投入するか」という taint boundary の
文脈であり、本 ADR が扱う「deterministic script と LLM semantic judgment のどちらに
どの判断を委ねるか」という役割分担の文脈とは異なる問題を扱っている。両者は互いに
supersede/矛盾する関係になく、本 ADR は 0007 を補完する位置づけとする。

## References（参考文献）

- Issue #2295（本 ADR を明文化する実装 Issue）
- Issue #2273（敵対的レビューで root cause が判明した実装 Issue、
  コメント https://github.com/squne121/loop-protocol/issues/2273#issuecomment-5378227355 、
  https://github.com/squne121/loop-protocol/issues/2273#issuecomment-5378300709 ）
- Issue #2294（本 ADR の parent、delivery-rollup tracking Issue、Child C/A/B の分割元）
- Issue #428（Step 3 adversarial review の例外ゲート化、narrow scope、Child A で統合要否を判断）
- Issue #1854（snapshot / preflight artifact の missing/stale/invalid/runtime error で
  implementation や PR publication を止めない、という既存方針）
- PR #2298 Owner レビューコメント（Option D 採用・severity/disposition 導入・
  advisory-by-default への改訂根拠、
  https://github.com/squne121/loop-protocol/pull/2298#issuecomment-5379760825 ）
- `.claude/skills/pr-review-judge/SKILL.md#AC6`（Deterministic Processing Script の禁止事項）
- `docs/dev/agent-skill-boundaries.md`
- `docs/adr/TEMPLATE.md`
- Claude Code 公式仕様: https://code.claude.com/docs/en/model-config#adjust-effort-level
