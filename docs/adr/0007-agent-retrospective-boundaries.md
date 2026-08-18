---
adr_id: "0007"
title: "agent-retrospective の run boundary・source authority・threat/trust matrix・mutation boundary・public/private artifact 境界"
summary_ja: "継続的 retrospective Skill（agent-retrospective）の run 境界・情報源の権威・脅威と信頼のマトリクス・変更操作の境界・公開/非公開成果物の境界を定める決定記録"
status: accepted
decision_date: "2026-08-19"
confirmed_date: "2026-08-19"
related_issues:
  - "#2192"
  - "#2234"
supersedes: []
superseded_by: null
---

# ADR 0007: agent-retrospective の run boundary・source authority・threat/trust matrix・mutation boundary・public/private artifact 境界

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

継続的 retrospective Skill（`.claude/skills/agent-retrospective/`、#2192）は、複数の
情報源（repository の Git 履歴、GitHub Issue/PR/comment/review/check、Web 参照情報）
を横断して「run」単位で観測し、改善候補（improvement candidate）を提案する Skill として
設計される。この Skill は 7 件の Child Issue（#2192 の Child 1〜7）に分割して実装される。

Child 2〜6（schema、source adapter、Skill/SubAgent 実装、永続化、セキュリティ境界の
自動テスト）が着手される前に、以下の 7 項目を「単なる説明文」ではなく、後続 schema が
機械化できる normative decision table として先に確定しておく必要がある。これらを
Child ごとの局所判断に委ねると、次の問題が生じる:

- run の「一貫性」を単一の atomic snapshot と誤認した実装（GitHub のようにページング
  される・時々刻々変化するソースを、Git の `base_sha` と同列に immutable として扱って
  しまう）
- 情報源間の対立（conflict）を単純な優先順位の上書きで解決してしまい、対立の性質
  （identity mismatch なのか、temporal mismatch なのか等）を失う実装
- untrusted な GitHub content（Issue/PR body・comment・title・branch/ref 名）や secret を
  実行可能コンテキストへ直接流してしまう実装（prompt injection・secret 漏洩）
- 「Issue コメントとして自動投稿する」ような mutation と、「improvement candidate を
  accepted にする・実装 Issue を起票する・repo ファイルを編集する」ような mutation を
  同列に扱い、human authorization の要否を誤って設計してしまう実装
- 「重複書き込みを防ぐ」（idempotency）と「古い前提での上書きを防ぐ」（optimistic
  concurrency）を混同し、片方だけを実装して他方の欠落に気づかない実装
- 「ブラックリスト方式（禁止文字列の除去）」で public 成果物を作ってしまい、想定外の
  機密情報が漏れる実装
- 既存の `agent_retro_index/v1`（`docs/dev/agent-retro-index.md`）と新設 schema の責務が
  重複し、どちらが正本か曖昧になる実装

本 ADR は、Child 2（#2235）の run identity 設計、Child 5（#2238）の永続化・idempotency
設計にも影響する先行決定であり、2026-08-18 OWNER レビュー（`in scope` reframe、
`docs/adr/0007-agent-retrospective-boundaries.md` の新規追加という Current Validated
Scope への収束）を反映して確定する。

## Considered Options（検討した選択肢）

1. **各 Child Issue の局所判断に委ねる**: schema/collector/persistence の実装時に、
   その場で run boundary や source authority を決める。実装速度は速いが、Child 間で
   矛盾した前提（例: Child 2 が atomic snapshot を仮定し、Child 5 が bounded observation
   を仮定する）が生じるリスクが高く、後から手戻りが大きい。採用しない。
2. **単一の全順序による source authority**（例: `repository > GitHub > runtime > web`
   の固定順位で常に上位を採用）: 実装は単純だが、claim の種類（例: 「このコードは
   いつ書かれたか」と「この PR はいつマージされたか」）によって信頼すべき情報源が
   異なるという現実を無視する。採用しない。
3. **claim-class 単位の source authority + conflict taxonomy**（本 ADR で採用）:
   claim の種類ごとに authoritative source を定義し、対立の性質を分類してから解決する。
   実装コストは選択肢 2 より高いが、Child 5 の idempotency 設計や Child 6 の automated
   verification の前提として必要な精度を持つ。
4. **「ブラックリスト方式」による public/private artifact 境界**（禁止文字列を除去して
   public 化する）: 実装は単純だが、想定していない機密情報の混入を防げない
   （false negative に対して構造的に脆弱）。採用しない。
5. **allowlist モデルによる public/private artifact 境界**（本 ADR で採用）: 既定で
   非公開とし、schema-controlled projection のみを public とする。実装コストは
   選択肢 4 より高いが、fail-closed な安全側の設計になる。

## Decision（決定事項）

以下 7 項目を、Child 2〜6 が実装時に参照する normative decision table として確定する。

### 1. Run consistency model（bounded observation・境界付き観測モデル）

各 run は単一の atomic snapshot ではなく、**`bounded observation`** として定義する。

- **repository source**（Git 履歴、リポジトリファイル）については、`base_sha` を持つ
  範囲は immutable snapshot として扱ってよい。`base_sha` が確定した時点で、その
  commit の内容は以後変化しない。
- **GitHub source**（Issue、PR、comment、review、check-run 等の paginated resource）
  および **Web source**（外部ドキュメント・API 応答等）は、同一時刻の atomic snapshot
  を取得できない。ページング中に新しい comment が追加される、review の状態が変化する
  等の理由で、収集完了時点と収集開始時点の状態が一致しない可能性が構造的に存在する。
  run 全体を単一の atomic snapshot だと誤認して扱ってはならない。
- source ごとに以下の evidence metadata を持たせる:
  - `observed_from` / `observed_until`: そのソースの観測を開始・終了した時刻
    （wall-clock ISO 8601、または API から取得できる更新時刻の範囲）
  - `query_params`: 適用したフィルタ・クエリパラメータ（例: `state=all`,
    `since=<timestamp>`）
  - `pagination_completeness`: 全ページを完走したか（`complete` /
    `partial` /  `unknown`）。`partial` の場合は打ち切り理由（rate limit、
    タイムアウト等）を残す
  - `cursor` / `etag`: 再現性・差分検知のための identity metadata（利用可能な場合）
- Child 2（#2235）の run identity（`base_sha` + timestamp + runtime_version +
  source_set_digest）は、このモデルに整合するよう、GitHub/Web source について
  `observed_from`/`observed_until` 等の evidence metadata を run identity とは別に
  source ごとに保持する必要がある（Remaining Parent Gaps 参照、追随修正が必要）。

### 2. Claim-class 単位の source authority

単一の全順序（`repository > GitHub > runtime > web` のような固定順位）を採用しない。
代わりに、claim の種類（`claim_class`）ごとに以下の対応表を定義する:

| claim_class | authoritative source(s) | corroborating source(s) | conflict policy | unavailable policy |
|---|---|---|---|---|
| `code_content`（コードの現在の内容） | repository（`base_sha` 時点） | なし（単一正本） | N/A（対立しない） | run を fail-closed で中断 |
| `code_authorship_timing`（いつ・誰が書いたか） | repository（`git log`/`git blame`） | GitHub（PR/commit metadata） | `temporal mismatch` として記録し repository を優先、GitHub 側の差分を注記 | `unavailable evidence` として記録し claim を保留 |
| `review_decision`（レビュー結果・承認状態） | GitHub（PR review/comment API） | なし | `identity mismatch`（review 対象 commit と現在の head が異なる）を検出したら claim を保留 | `unavailable evidence` として記録 |
| `issue_intent`（Issue の意図・スコープ） | GitHub（Issue body、最新版） | GitHub Issue comments（refinement 履歴） | `semantic disagreement`（body と comment 履歴の解釈が割れる）を記録し body を優先、comment の異論を注記 | `unavailable evidence` として記録 |
| `external_fact`（外部ライブラリ仕様・ドキュメント記載等） | Web（一次情報 URL） | なし | `semantic disagreement` を記録し claim を保留、human 判断を要求 | `unavailable evidence` として記録し claim を保留 |
| `runtime_behavior`（実行結果・テスト結果） | runtime（実行ログ・test artifact） | repository（テストコード内容） | `temporal mismatch`（実行時点と評価対象 commit のズレ）を検出したら claim を保留 | `unavailable evidence` として記録し claim を保留 |

対立（conflict）は以下 4 種に分類し、単純な優先順位による上書きではなく、この分類に
基づいて解決する:

- **`identity mismatch`**: 比較対象の identity（commit SHA、PR head、review 対象）が
  一致していないために生じる対立（例: 古い commit に対する review を最新 commit の
  claim として扱おうとした）。
- **`temporal mismatch`**: 観測時刻のズレによって生じる対立（例: run 実行後に PR が
  更新され、収集済みの情報が古くなった）。
- **`semantic disagreement`**: identity・時刻は一致しているが、情報源同士の主張内容が
  意味的に食い違う対立（例: Issue body の記述と comment 履歴の解釈が異なる）。
- **`unavailable evidence`**: 対立ではなく、必要な情報源にそもそもアクセスできない
  状態（rate limit、権限不足、404 等）。claim を確定せず保留として扱う。

## 3. Threat/trust matrix（脅威・信頼マトリクス）

`asset × trust level × capability × sink × mitigation` の形式で少なくとも以下を扱う。

| asset | trust level | capability（何ができるか） | sink（どこに流れうるか） | mitigation |
|---|---|---|---|---|
| malicious Issue/PR body（untrusted GitHub content） | untrusted（外部投稿者が制御可能） | 任意のテキストを埋め込める | LLM プロンプトコンテキスト、Issue/PR comment 生成、shell コマンド引数 | untrusted content を実行可能コンテキスト（shell コマンド、ツール引数構築）へ直接連結しない。テキストとしてのみ LLM コンテキストに渡し、そこから抽出した「指示」をそのまま権限のある操作へ変換しない（GitHub のスクリプトインジェクション防御方針、Claude Code の prompt injection 防御方針〔permission・isolation・sanitization〕と整合） |
| malicious comment（untrusted GitHub content） | untrusted | 任意のテキスト・リンクを埋め込める | 同上 | 同上 |
| malicious title（untrusted GitHub content） | untrusted | 短文だが同様のリスク | 同上 | 同上 |
| malicious branch/ref 名（untrusted GitHub content） | untrusted | shell 引数として解釈されうる文字列を含められる | `git` コマンド引数、shell 実行 | shell 引数は配列渡し（`shell=False` 相当）で構築し、branch/ref 名を shell 文字列連結で組み立てない |
| secret（API token、credential） | confidential（repo/env に存在する場合は trusted source だが公開してはならない） | 存在するだけで漏洩リスクを持つ | public artifact（Issue comment、PR body、public な成果物ファイル） | public/private artifact 境界（下記 6 項）の allowlist モデルに従い、schema-controlled projection にのみ含まれることを保証する。secret を含みうる raw evidence は既定で非公開 |
| absolute local path | 準機密（実行環境・ユーザー情報を推測されうる） | 環境情報の漏洩 | 同上 | 同上（allowlist モデルで非公開） |
| raw stdout/stderr（tool 実行結果） | untrusted〜準機密混在（外部コマンドの出力は untrusted content を含みうる） | secret・path の漏洩、prompt injection の再入力経路 | LLM プロンプトコンテキスト、public artifact | raw stdout/stderr を schema-controlled projection を経ずに public artifact へ転記しない。LLM コンテキストへの再投入時も untrusted content として扱う |
| prompt injection payload（Issue/PR/comment/Web に埋め込まれた LLM 向け指示文字列） | untrusted | LLM の判断・以後のツール呼び出しを誘導しうる | LLM プロンプトコンテキスト、以後の mutation 判断 | untrusted external content を「データ」としてのみ扱い、そこに含まれる指示文をそのまま権限のある操作（mutation boundary 下記 4 項の remediation/control-plane mutation）の承認根拠にしない。mutation は本 ADR の mutation boundary（human authorization point）に従う |

本 matrix は、GitHub の script injection 防御方針（Actions のようにコンテンツを
直接シェル評価しない）および Claude Code の prompt injection 防御方針
（permission・isolation・sanitization の三層）と整合する形で運用する。

## 4. Mutation boundary（artifact-publication mutation と remediation の区別・変更操作の権限境界）

mutation を次の 2 分類に明示的に分け、それぞれの human authorization point を定義する。

- **`artifact-publication mutation`**: retrospective run の観測結果そのものを記録・
  公開する mutation。例: `agent_retrospective_run/v1` を Issue comment として投稿する
  こと。これは「run が何を観測したか」を記録する行為であり、repo のコード・設定・
  runtime の挙動を変更しない。human authorization point: **事前承認不要（自動投稿を
  許可してよい）**。ただし投稿内容は本 ADR の public/private artifact 境界
  （下記 6 項の allowlist モデル）を満たしたものに限る。
- **`remediation / control-plane mutation`**: retrospective の結果を受けて、状態や
  repo に対して変更を加える mutation。例: improvement candidate を `accepted` 状態に
  遷移させる、実装 Issue（implementation issue）を新規作成する、repo ファイルを編集
  する。これらは repo の将来の挙動・作業キューを変更する行為である。human
  authorization point: **事前の人間承認が必要（proposal-only）**。retrospective
  Skill は improvement candidate を `proposed` 状態で提示するに留め、`accepted` への
  遷移・実装 Issue の起票・repo ファイル編集を自動で行わない。

Child 5（#2238）が実装する `agent_retrospective_run/v1` の Issue comment 自動投稿は
`artifact-publication mutation` に該当し、事前承認不要で自動投稿してよい。これは
「improvement candidate を提案のみに留める」という proposal-only の原則と矛盾しない
（run の観測結果を記録することと、改善候補を承認することは別の mutation class である
ため）。

## 5. Optimistic concurrency と idempotency の区別

この 2 つは異なる問題を解決する、異なる仕組みとして区別する。

- **idempotency（duplicate suppression）**: 同一の入力に対する重複書き込みを拒否する
  仕組み。`(repo, base_sha, source_set_digest, scope)` の組が既存の記録と一致する場合、
  再度同じ内容を書き込まない。これは「同じことを 2 回書かない」ための保護であり、
  「他の誰かが書いた最新の状態を見落としていないか」は検証しない。
- **optimistic concurrency（stale-write protection）**: ある run（run A）が読んだ状態を
  前提に書き込もうとした時点で、その間に別の run（run B）がその状態を更新していた
  場合、run A の古い前提での上書きを防ぐ仕組み。`expected_previous_digest` /
  `version` / `comment identity` 等、書き込み対象の「前提バージョン」を明示し、
  書き込み時点の実際の状態と照合してから書き込む（compare-and-swap 相当）。

idempotency は「同一内容の重複」を防ぎ、optimistic concurrency は「古い前提での上書き」
を防ぐ。両者は独立した mutation boundary の contract であり、片方を実装しても他方の
代替にはならない。この区別は persistence 実装（Child 5）の局所判断に委ねず、本 ADR で
先に定義する。

Child 5（#2238）の現行実装は `(repo, base_sha, source_set_digest, scope)` ベースの
重複抑止を実装済みだが、これは idempotency であって stale-write protection
（optimistic concurrency）ではない（2026-08-18 OWNER レビューで判明。Remaining
Parent Gaps 参照）。真の optimistic concurrency（`expected_previous_digest`/
`version` 等）は Child 5 側の追随修正として別途設計する。

## 6. Public/private artifact 境界（allowlist モデル）

「禁止文字列のブラックリスト」方式（既知の機密パターンを除去してから公開する）は
採用しない。代わりに **allowlist モデル**を採用する: raw evidence は既定で非公開とし、
public な成果物は schema で定義された field のみを含む **schema-controlled
projection** に限定する。

非公開対象（public artifact に含めてはならない raw evidence）の例:

- raw transcript（LLM とのやり取りの生ログ全体）
- secret（API token、Authorization header、cookie、credential-bearing URL）
- absolute local path
- environment value（環境変数の値）
- private GitHub content（private repository の内容、非公開 Issue/PR、限定公開情報）
- PII（個人を特定しうる情報）
- tool の stdout/stderr（生の実行結果）
- prompt injection payload（そのまま転記すると再発火しうる指示文字列）

schema-controlled projection（public 化してよい対象）は、Child 2 が定義する
`agent_retrospective_run/v1` / `agent_improvement_candidate/v1` の schema が明示的に
定義した field のみとする。schema に定義されていない field を「とりあえず含める」
判断を実装時に行わない（allowlist の原則）。

## 7. 既存 `agent_retro_index/v1` との責務分離

`agent_retro_index/v1`（`docs/dev/agent-retro-index.md`）は **derived index**（既存の
retrospective 記録から導出される索引）としての役割を維持し、本 ADR が扱う run の
実行状態・improvement candidate の状態（state）を持たせない。

- run/candidate の一次記録（state）は、Child 2（#2235）が新設する
  `agent_retrospective_run/v1` / `agent_improvement_candidate/v1` の 2 schema が持つ。
- `agent_retro_index/v1` は、これら新設 schema から導出された索引情報（要約・
  リンク集）を保持する既存の役割のまま維持し、state の正本にはしない。
- Child 2〜6 の実装は、`agent_retro_index/v1` を state の書き込み先として使わない。

## Consequences（帰結）

**肯定的影響**:

- Child 2（run identity/schema）、Child 5（persistence）、Child 6（automated
  verification）が、矛盾する前提の上に実装される事態を防げる。
- claim-class 単位の source authority と conflict taxonomy により、Child 6 の
  automated verification が「対立の種類」を機械的に分類できる。
- mutation boundary の明確化により、Child 4（Skill/SubAgent 実装）が
  `artifact-publication mutation` の自動投稿と `remediation` の人間承認を型として
  区別しやすくなる。
- allowlist モデルの採用により、schema に未定義の field が誤って public 化される
  リスクを構造的に下げる。

**否定的影響・トレードオフ**:

- claim-class 単位の対応表・conflict taxonomy・allowlist モデルは、単一全順序や
  ブラックリスト方式と比べて実装コストが高い。
- bounded observation モデルにより、GitHub/Web source からの claim は「確定」ではなく
  「保留され得る」ケースが増え、Skill の提案が保守的になる可能性がある。

**後続 Issue / ADR への引き継ぎ事項**（Remaining Parent Gaps より）:

- Child 5（#2238）: `(repo, base_sha, source_set_digest, scope)` ベースの重複抑止を
  idempotency として明示し、真の optimistic concurrency（`expected_previous_digest`/
  `version` 等）を別途設計する追随修正が必要。
- Child 5（#2238）: `agent_retrospective_run/v1` の Issue comment 自動投稿契約を、
  本 ADR の mutation boundary（`artifact-publication mutation` に該当する旨）に
  合わせて再確認する追随修正が必要。
- Child 2（#2235）: run identity（`base_sha` + timestamp + runtime_version +
  source_set_digest）を、本 ADR の bounded observation モデル（source ごとの
  `observed_from`/`observed_until` 等の evidence metadata）に合わせて再確認する
  追随修正が必要。
- Child 7（plugin distribution）: Child 4 完了後に着手する。

## References（参考文献）

- #2192（親 Issue: 継続的 retrospective Skill 実現）
- #2234（本 Issue: 本 ADR の起票元）
- #2235（Child 2: schema 実装、本 ADR の run identity / claim-class 定義を前提とする）
- #2238（Child 5: 永続化・idempotency 実装、本 ADR の mutation boundary / optimistic
  concurrency 区別を前提とする）
- `docs/dev/agent-retro-index.md`（既存 `agent_retro_index/v1`。derived index として
  維持し、本 ADR の run/candidate state とは責務分離する）
- `docs/adr/TEMPLATE.md`（本 ADR の frontmatter 契約の正本）
- `docs/dev/agent-skill-boundaries.md`（本 ADR への参照リンクを追記）
- 2026-08-18 OWNER レビュー
  （https://github.com/squne121/loop-protocol/issues/2234#issuecomment-5327616033、
  verdict: REQUEST_CHANGES / reframe_in_place）
