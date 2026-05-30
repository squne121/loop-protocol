---
adr_id: "0003"
title: "Schema Catalog 機械可読化の採用方針（C+: catalog 導入 + SSOT 移行）"
status: accepted
decision_date: "2026-05-30"
confirmed_date: null
related_issues:
  - "#470"
  - "#131"
  - "#135"
  - "#167"
  - "#169"
  - "#170"
supersedes: []
superseded_by: null
---

# ADR 0003: Schema Catalog 機械可読化の採用方針（C+: catalog 導入 + SSOT 移行）

## Context

PR #81 で `delegation_result/v1` schema の正規化により downstream consumer が silent に壊れた事故を受け、#135 / #167 で `docs/dev/schema-governance.md`（Markdown テーブル）と PR テンプレートへの Schema Consumer Inventory 義務を導入した。

しかし、現行の Markdown テーブル管理には以下の構造的な問題がある:

1. **手続きへの依存**: `rg` コマンドによる consumer 列挙を PR 作成者・AI が手動で行う必要があり、漏れを機械的に止められない
2. **validation の未実装**: `validate_pr_body.py` の LP050 は `# before` / `# after` ヘッダと consumer テーブル列名の文字列存在確認のみで、catalog との照合・consumer 完全性検証を行っていない
3. **推定エントリの混在**: `model_routing.yaml（推定）` のような曖昧な登録が許容されており、machine-readable catalog では禁止すべき状態
4. **#170 の限界**: `E_SCHEMA_CONSUMER_INVENTORY_MISSING` は `LP050`/`LP052` の error-code 分類のみで、catalog との照合は未実装

#470 では「Markdown テーブルを機械可読 catalog に移行し、producer-consumer 依存を検証可能にする設計を決定する」ことを目的として以下の RQ を設定した。

## Research Questions の調査結果

### RQ1: 現状の schema 数は catalog 化に見合うか

`docs/dev/schema-governance.md` の Initial Known Schemas テーブルには現在 **16 エントリ**（2026-05-30 時点）が登録されている:

- `issue_contract/v1`, `delegation_request_v1`, `delegation_result/v1`, `acp_result_v1`
- `LOOP_VERDICT`, `TEST_VERDICT_MACHINE v1`, `IMPLEMENT_RESULT_V1`
- `contract_schema_version: v1`, `Runtime Verification Applicability`, `Safety Claim Matrix`
- `model_routing.yaml`, `runtime-verification artifact log`
- `pr_body_schema/schema_change_applicability/v1`, `pr_body_schema/schema_consumer_inventory/v1`
- `agent_session_manifest/v1`, `PR_REVIEW_GATE_RESULT_V1`

このうち `agent_session_manifest/v1` のみ詳細登録（definition_paths / detection_patterns / validation_commands）が存在し、残りは最小メタデータのみ。

**判定**: 16 エントリは catalog 化に見合う。管理対象として十分な規模であり、手動 Markdown テーブルでは consumer 更新漏れを防ぐのに限界がある。`model_routing.yaml（推定）` のような曖昧エントリが存在しており、機械可読 catalog への移行で精度が向上する。

### RQ2: `schemas/catalog.yaml` 形式で `open_pr.py` の guard と統合できるか

`validate_pr_body.py` の LP050 は現在文字列存在確認に留まっているが、以下の拡張で catalog との統合が技術的に可能:

1. **catalog completeness check**: PR body の Schema Consumer Inventory に記載された schema_id が catalog に存在するか
2. **consumer consistency check**: PR body の consumer 列挙が catalog の `consumers[].paths` と一致するか
3. **detection_patterns check**: changed files が catalog の `detection_patterns` にマッチするか確認し、未記載 schema_id を検出

統合のアプローチ: `validate_schema_catalog.py` を追加し、LP050 から呼び出す。catalog ファイルのパスは `schemas/catalog.yaml` とする。

**判定**: 統合は可能。C+ アプローチでは LP050 の拡張として段階的に実装できる。

### RQ3: Buf 相当の breaking change detection を JSON Schema / PR body contract に移植するコストは

Buf の breaking change detection は Protobuf schema 専用であり、client/server/generated code/wire/JSON encoding の各層に対して rule category を選択できる成熟したツールである。

JSON Schema に対しては同等の標準ツールが存在しない:
- `json-schema-org/community` の GSoC 2026 proposal として「JSON Schema には公式の breaking change checker がない」と整理されている
- `getsentry/json-schema-diff` は README で work-in-progress / best-effort / 未実装 keyword が多いと明記

また、本プロジェクトの schema の多くは Markdown YAML contract / PR body contract であり、JSON Schema ではない。これらに Buf 相当の semantic diff を適用するのはさらに困難。

**判定**: Buf 相当の semantic breaking detection を JSON Schema / PR body contract に移植するコストは高い。初期版では以下の段階的アプローチを採用する:

| チェック | 実装形態 | blocking |
|---|---|---|
| catalog completeness check | validate_schema_catalog.py | blocker |
| PR body consumer consistency check | LP050 拡張 | blocker |
| fixture execution check | pytest / uv run pytest | blocker |
| semantic breaking check (JSON Schema) | best-effort / warning のみ | warning（blocker 化しない） |

### RQ4: Pact の consumer-driven contract testing の考え方を PR body fixture に適用できるか

Pact 公式は consumer test について「API client の良い unit test を書くことが出発点」「generic HTTP client ではなく実際の consumer code を通す必要がある」と説明している。

PR body fixture への適用では、Markdown snapshot を置くだけでは不十分。fixture は以下のパイプラインを通す必要がある:

```text
PR body fixture
  -> validate_pr_body.py
  -> open_pr.py dry-run（将来）
  -> pr-review-judge gate checker
  -> impl-review-loop が読む stdout / verdict contract
```

つまり、catalog は「consumer_patterns の一覧」ではなく「consumer が実行する検証コマンドの一覧（`required_test_commands`）」を持つ必要がある。

**判定**: Pact の考え方は適用可能だが、fixture が consumer コードパス（validate_pr_body.py 等）を通すことが前提。`required_test_commands` フィールドを catalog に持たせることで、consumer 側の検証を自動化できる。

## Considered Options

**Option A**: `schemas/catalog.yaml` を完全採用し、`#170` の guard と即時統合
- メリット: 完全な機械可読 catalog
- デメリット: JSON Schema の semantic breaking detection が未成熟、Buf 相当を短期で実装するのは過剰なコスト

**Option B**: 現行の Markdown テーブル + open_pr.py guard で十分と判断し、catalog 移行は不採用
- メリット: 追加実装なし
- デメリット: LP050 が文字列確認のみという構造的限界が残る。consumer 更新漏れを機械的に止められない

**Option C**: 機械可読 catalog は PR body contract 検証にのみ使う（部分採用）
- メリット: 範囲を限定できる
- デメリット: SSOT が Markdown と YAML に分裂する

**Option C+**: `schemas/catalog.yaml` を導入し schema-governance.md の SSOT を catalog に移行。open_pr.py / validate_pr_body.py は catalog と PR 本文の Schema Consumer Inventory を照合する。semantic breaking detection は JSON Schema 限定の best-effort warning とし blocker 化しない。follow-up Issue を分割して CI / pytest 整備を続ける
- メリット: SSOT の一元化、段階的実装が可能、現実的なコスト
- デメリット: Markdown テーブルの廃止と catalog への移行工数が必要

## Decision

**採用方針: C+**

理由:

1. **SSOT の一元化が最優先**: `docs/dev/schema-governance.md` の Markdown テーブルと `schemas/catalog.yaml` の二重 SSOT は避ける。catalog を導入するなら Markdown 側の Initial Known Schemas テーブルは catalog から生成する形に移行する
2. **semantic breaking detection は best-effort**: JSON Schema の互換性 checker は成熟していない。`breaking_change_policy` を「自動で判定できる」と過大設計せず、warning から開始する
3. **consumer 検証コマンドを catalog に持たせる**: consumer_patterns の一覧だけでなく `required_test_commands` を持つことで、Pact の考え方（consumer code を通す）を実現する
4. **段階的実装**: catalog schema → validator → PR body 照合 → fixture tests → Markdown 生成 の順で follow-up Issue に分割する

### `schemas/catalog.yaml` の最小スキーマ定義

各エントリの必須フィールド:

```yaml
schema_id: "<string>"           # 一意識別子（例: delegation_result/v1）
format: markdown_yaml_contract | json_schema | yaml | ndjson | markdown_table
definition_paths:               # schema が定義されているファイルパス（複数可）
  - "<path>"
producer:
  owner: "<string>"             # 所有チーム or skill 名
  paths:                        # producer のソースパス
    - "<path>"
consumers:
  - id: "<string>"              # consumer の識別子
    paths:                      # consumer のソースパス
      - "<path>"
    detection_patterns:         # consumer が参照する文字列パターン（rg 検索用）
      - "<pattern>"
    required_test_commands:     # consumer 側の検証コマンド
      - "<command>"
compatibility:
  mode: manual_policy           # 初期版は manual_policy のみ
  direction: backward | forward | full | custom
  breaking_changes:             # breaking change とみなす変更種別
    - remove_required_field
    - rename_field
    - narrow_type
validation:
  catalog_lint:                 # catalog 自体の整合性チェックコマンド
    - "<command>"
  fixture_tests:
    positive: []                # 正常系 fixture ファイルパス
    negative: []                # 異常系 fixture ファイルパス
migration:
  required_for_breaking_change: true
  followup_issue_required: true
last_verified:
  commit: "<sha>"
  command: "<command>"
```

JSON Schema を扱う場合は `$schema`（draft 宣言）と `$id`（絶対 URI による一意識別子）を必須とする。

### catalog completeness check の実装方針

`validate_schema_catalog.py` を `.claude/skills/open-pr/scripts/` に追加し、以下を検証する:

1. **catalog completeness**: schema_id / format / definition_paths / producer / consumers / detection_patterns / validation_commands が埋まっているか（`推定` 等の曖昧値を禁止）
2. **PR body consistency**: PR 本文の Schema Consumer Inventory が catalog の `consumers[].id` と矛盾していないか
3. **fixture execution**: 変更対象 schema の positive / negative fixture を実行

LP050 から `validate_schema_catalog.py` を呼び出し、エラーは `E_SCHEMA_CATALOG_MISSING` / `E_SCHEMA_CONSUMER_MISMATCH` として分類する。

### semantic breaking detection の境界定義

| 判定 | 分類 | 対応 |
|---|---|---|
| JSON Schema の field 追加（optional） | 非 breaking | pass |
| JSON Schema の required field 削除・rename | 推定 breaking | **warning**（blocker ではない）|
| Markdown YAML contract の key 名変更 | 推定 breaking | **warning** |
| 自動判定不能 | unknown | **manual review 必須**（PR body に理由記載を要求）|

初期版では semantic breaking detection を blocker 化しない。#169（AI 遵守保証）が post-M1 に defer されているため、semantic diff の精度が担保できない状態で blocker にするとフォールスポジティブが多発する。

### schema-governance.md との SSOT 移行方針

1. `schemas/catalog.yaml` を導入し、全 schema の正本を catalog に移行する
2. `docs/dev/schema-governance.md` の Initial Known Schemas テーブルは `scripts/generate_schema_governance.py`（follow-up）で catalog から生成する
3. 移行完了後、Markdown テーブルのセクションを「catalog から生成」と注記し、直接編集を禁止する

## Consequences

### 肯定的影響

- consumer 更新漏れを機械的に止められる仕組みの基盤ができる
- `推定` エントリが排除され、schema 登録の信頼性が向上する
- SSOT が一元化され、Markdown と YAML の二重管理が解消される
- `agent_session_manifest/v1` の詳細登録形式を全 schema に拡張できる

### 否定的影響 / トレードオフ

- catalog への初期移行工数が必要（16 エントリ × フィールド展開）
- `validate_schema_catalog.py` の実装・テスト工数が発生する
- semantic breaking detection の精度は初期版では限定的（warning のみ）

### 後続 Issue への引き継ぎ

下記の follow-up Issue を分割して起票する（AC7 対応）:

| # | タイトル | 内容 |
|---|---|---|
| FU-1 | catalog schema + initial migration | `schemas/catalog.yaml` 作成と 16 エントリの移行 |
| FU-2 | catalog consistency validator | `validate_schema_catalog.py` 実装 |
| FU-3 | PR body inventory vs catalog 照合 | LP050 拡張：catalog との consumer 整合性チェック |
| FU-4 | fixture-based consumer contract tests | positive / negative fixture と pytest 追加 |
| FU-5 | schema-governance.md 生成スクリプト | catalog から Markdown テーブルを生成 |

## References

- Issue #470（本 ADR の親 Issue）
- Issue #131（schema deterministic enforcement 親 tracker）
- Issue #135（Schema Consumer Inventory 義務化）
- PR #167（schema-governance.md と governance ルール追加）
- Issue #169（AI 遵守保証 / post-M1 defer）
- Issue #170 / PR #478（`E_SCHEMA_CONSUMER_INVENTORY_MISSING` error code 追加）
- `docs/dev/schema-governance.md`（現行 SSOT）
- JSON Schema GSoC 2026 proposal: https://github.com/json-schema-org/community/issues/984
- Buf breaking change docs: https://buf.build/docs/breaking/
- Pact consumer docs: https://docs.pact.io/consumer
