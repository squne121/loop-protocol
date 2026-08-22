---
adr_id: "0008"
title: "Deterministic Evidence vs Model-Based Semantic Judgment の役割分担"
summary_ja: "決定論的に確認できる事実は script に委譲し、意味論的な設計判断は LLM の推論に委ねるという役割分担原則を定める決定記録。issue-refinement-loop が semantic design finding を未解決のまま処理完了扱いにした構造的欠陥（#2273）を踏まえ、deterministic gate と LLM semantic review lane の併用を採用する。"
status: proposed
decision_date: null
confirmed_date: null
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

### Option C: deterministic gate + LLM semantic review lane を併用する（採用）

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

## Decision（決定事項）

Option C（deterministic gate + LLM semantic review lane 併用）を採用する。

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
2. **決定論的に判定できないことを、それ自体で human escalation の理由にしない**。
   LLM が repository evidence、外部一次資料、Owner Decision、Issue/PR history から
   十分な根拠を得て合理的に決定できる場合は、LLM が判断・修正案の作成まで担当する。
   human escalation は次のような genuinely human-only なケースに限定する。
   - 複数の合理的な設計案の間で Owner の価値判断が必要な場合
   - canonical な Owner Decision 同士が矛盾している場合
   - 現在の Agent/SubAgent の authority が不足している場合
   - bounded 調査を尽くしても dispositive な evidence が得られない場合
   - Owner が明示的に人間判断を要求した場合
3. **loop 終了ゲートは semantic review lane の未解消 finding を考慮する**。deterministic
   checker が `approve`/`go` を返しても、semantic review lane が検出した finding が
   未解消のまま loop を正常終了させてはならない。終了判定の入力に「semantic finding の
   解消状態」という事実を追加する（finding の意味内容の再判定は不要で、
   「解消済みか未解消か」という deterministic に確認可能な状態のみを見ればよい）。
「解消済みか未解消か」の値は、semantic review lane（LLM）が現在の Issue body / diff に
対して都度再実行し出力した finding state（sidecar artifact のフィールド、run id +
body_sha256 束縛）としてのみ確定させる。deterministic code は当該フィールドの
schema 妥当性・現行 body_sha256 との freshness 一致・provenance のみを検証し、diff の
有無・キーワードの消失・anchor comment の残存有無などから解消状態を自ら推測・再計算
してはならない。これは規範1（deterministic script に semantic verdict を生成させない）の
適用範囲に「finding の解消判定」も含まれることを明示するものである。
4. **semantic review lane の追加は既存の固定 wire フォーマットを破壊しない**。
   `ISSUE_REVIEW_RESULT_COMPACT_V2`（固定行数の wire）や
   `LOOP_REWRITE_ROUTER_STATE_V1`（`additionalProperties: false`）に直接フィールドを
   追加せず、新規の sidecar artifact として分離する（具体的なフィールド設計は
   Child A（#2296）の scope）。

## Consequences（帰結）

### Child A（semantic design review lane 実装、#2296、本 ADR に blocked_by）への引き継ぎ事項

- `issue-refinement-loop` に「Step 2.5」として semantic design review lane を追加する。
  過去に不採用となった「Step 3」（#428 の adversarial review 案）という番号は再利用しない。
- semantic review 用の新規 SubAgent は Sonnet 5 に `effort: high` を指定する
  （Claude Code 公式仕様 https://code.claude.com/docs/en/model-config#adjust-effort-level
  で対象 model への effort 指定が正式サポート済みと確認済み）。
- 新規 sidecar artifact（`semantic_review_result_v1` 等）は既存の固定 wire フォーマット
  （`ISSUE_REVIEW_RESULT_COMPACT_V2` / `LOOP_REWRITE_ROUTER_STATE_V1`）を変更せずに追加する。
- loop 終了ゲート（`decide_next_loop_action.py` 等）に、semantic review lane の
  未解消 finding を終了判定の入力として追加する。
- #428（Step 3 adversarial review の例外ゲート化、narrow scope）との重複要否を
  Child A 実装時に判断し、重複する場合は #428 を close するか、#428 の残存部分を
  Child A に統合するかを決める。

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
- `.claude/skills/pr-review-judge/SKILL.md#AC6`（Deterministic Processing Script の禁止事項）
- `docs/dev/agent-skill-boundaries.md`
- `docs/adr/TEMPLATE.md`
- Claude Code 公式仕様: https://code.claude.com/docs/en/model-config#adjust-effort-level
