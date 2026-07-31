---
adr_id: "0006"
title: "Profile provider contract capability design（承認設計）"
summary_ja: "profile/provider 契約の能力・認証・安全性・運用状態を分離し、GitHub 権限境界と後続実装契約を固定する設計判断。"
status: proposed
decision_date: null
confirmed_date: null
related_issues:
  - "#1821"
  - "#1814"
supersedes: []
superseded_by: null
---

# ADR 0006: Profile provider contract capability design

## Status
- `status`: proposed
- `decision_owner`: Issue #1821
- `state`: design-only（実装・検証は含まない）
- `decision_effective_date`: 未設定

## Context
Loop Protocol の profile provider 処理において、タスクインテント、capability、認証、権限、監査、移行ルールを単一 ADR で明文化し、実装依存の解釈ぶれを防ぐ必要がある。

#1814 は現在 OPEN であり、当該 Issue での実装可否や自動実行適格性を「完了済み」と誤認しない設計記述が必要である。

同時に、Profile Provider は既存の schema v1 参照・検証観点（JSON Schema / semantic validator / fixtures）との整合を保ちながらも、本 Issue では ADR 以外の実装を追加しない前提で進める。

## Decision
- 本 Issue では `profile provider` の契約を自然言語仕様としてのみ確定し、実装は後続 Issue に委譲する。
- Capability 設計は「最低権限」「明示的拒否」「禁止解釈」をセットで定義する。
- GitHub 利用権限は issue 固有の表形式で明示し、`issue`/`pull requests`/`comments`/`reviews`/`contents`/`workflows`/`merge` を明確な別行で記録する。
- `github_authoring` は proposal/output mode 扱いとし、ここで capability/権限の実施前提を付与しない。

## Profile/task intent
- Profile 表示・参照・切替・整合確認の範囲内でのみ意図を扱う。
- Task は `resolve intent -> authorize -> present candidate -> return deterministic profile decision` を 1 パスで行う。
- 実行時状態は `dry-run 可能`、`mutation は issue 監査付きジョブでのみ`、それ以外は `read-only` を原則とする。

### 禁止解釈
- この ADR を、認証を跨いだ無制限実行の許可や、未承認環境への直接 mutation 根拠と解釈しない。

## Capability sets
- `identity_profile_capability`: プロファイル識別、状態遷移意図の取得、保護対象属性の read。
- `provider_profile_capability`: プロバイダ固有パラメータの正規化、policy-aligned な候補評価、差分説明の生成。
- `audit_profile_capability`: 判定時の根拠保存、policy tag 出力、結果コードの分類（allow/deny/challenge）。
- `routing_profile_capability`: 例外時の代替導線（ユーザ確認待ち／再試行）へ明示的に遷移。

### 禁止解釈
- Capability を一括実行の万能スイッチとして扱わない。
- `identity_profile_capability` をもって credential 取得や workflow mutation を代行する用途に使わない。

## Credential permissions
- `identity_profile_capability` は token の「識別子参照」レベルのみ。
- `provider_profile_capability` は最小スコープの credential binding を行うが、保存・再配布しない。
- `audit_profile_capability` は credential 自体を保存せず、利用履歴のみを metadata 表現に残す。
- `routing_profile_capability` は credential 値を要求しない。

### 禁止解釈
- credential を永続化保存領域に退避し続ける実装を許容しない。
- 権限昇格前提（implicit privilege escalation）を許可しない。

## Safety enforcement
- すべての mutation 判定前に 1) policy 適用 2) 失敗時 reason コード 3) fallback 判定 を必須化。
- `dry-run` が明示されない限り、未検証パスでの mutation は禁止。
- 異常時は `challenge` を返し、明示的な再試行イベントがなく auto-continue しない。

### 禁止解釈
- 例外や運用判断を理由に safety check を省略しない。
- ログ圧縮のため監査証跡を省略してよいとの前提は不可。

## Auth
- ユーザー/システムの認証は `intent source` と `request actor` を分離して追跡。
- 既定状態は最小権限トークンのみ。
- 権限外 API 呼び出し要求は即時拒否し、代替導線に遷移。

### 禁止解釈
- `auth` を単なる有無（true/false）でなく、権限と目的の適合性を前提とする。
- 1 回の認証結果を他トランザクションへ再利用する暗黙キャッシュを前提としない。

## Operational state
- `Idle` / `Resolving` / `Awaiting_approval` / `Applying` / `Failed` / `Completed` の 6 状態を定義。
- `Applying` は安全ゲート完了後にのみ遷移。
- `Failed` では mutation 成果をロールバックし、`awaiting action` を最短1行で説明する。
- 状態遷移は監査記録に必ず残し、外部表示は最小集合（状態名＋reason）に限定。

### 禁止解釈
- `Applying` 中断を「成功済み」と混同しない。
- `Completed` を再試行成功の無条件証拠とみなさない。

## Routing policy
- policy 決定は `read-only route`, `approval route`, `challenge route` の3系統。
- 各ルートで必要な入力（intent, risk, auth context）を最小項目で固定。
- `approval route` のみ外部 side effect（mutation）を許可。

### 禁止解釈
- すべてのルートを単一化し、監査不能な自動 apply に収束させない。

## Profile Provider GitHub Grants（GitHub 権限付与の個別判断表・設計上の必須区分: action / endpoint family / permission / grant-or-deny / mutation / rationale / #1814 impact）

| action | endpoint family | permission | grant-or-deny | mutation | rationale | #1814 impact |
|---|---|---|---|---|---|---|
| metadata | repository metadata | metadata: read | grant（設計上の参照候補） | no | provider 選択時に repository identity を照合するため | `#1814` OPEN 中は runtime grant・実装済みを主張しない |
| issues | issues | issues: read | grant（設計上の参照候補） | no | task intent と既存 Issue 状態を確認するため | `#1814` OPEN 中は mutation / verified-write を主張しない |
| pull requests | pull requests | pull-requests: read | grant（設計上の参照候補） | no | 関連 PR の状態と比較対象を参照するため | `#1814` OPEN 中は auto-eligible を主張しない |
| comments | issue comments / pull request comments | issues: read / pull-requests: read | grant（設計上の参照候補） | no | owner intent と監査説明の既存記録を参照するため | `#1814` OPEN 中は verified write を主張しない |
| reviews | pull request reviews | pull-requests: read | grant（設計上の参照候補） | no | safety rationale と review state を参照するため | `#1814` OPEN 中は mutation verified を主張しない |
| contents | repository contents | contents: read | grant（設計上の参照候補） | no | 設定・契約を参照専用で取得するため | `#1814` 未完了のため contents write を前提化しない |
| workflows | actions / workflows | actions: read | deny | no | workflow の参照・起動はこの設計契約の対象外とするため | `#1814` OPEN 中は workflow mutation の実装済み主張を禁止 |
| merge | pull requests merge | pull-requests: write / contents: write | deny | no | merge は別 Issue の専有領域であり、この ADR は許可しないため | `#1814` OPEN 中は mutation implemented を記載不可 |

`github_authoring` は本 Issue の範囲では proposal/output mode とみなし、候補文・出力を生成できることだけを表す。上表の action、endpoint、permission、grant を含意せず、GitHub capability でも runtime credential grant でもない。

## #1814 guardrails
- `#1814` が OPEN の間、以下の断定は本 Issue で禁止する。
  - `mutation implemented`
  - `mutation verified`
  - `verified-write`
  - `auto-eligible`
  - `ready-to-apply`（監査証跡が未揃いの自動適用示唆）
- `#1814` が完了するまで、設計上の表現は `pending`, `proposed`, `requires follow-up` に限定。
- 監査で conflict が発生した場合、既存ルールに従い先に guardrail 違反として停止し、追加実装には進まない。

## Follow-up contract
- 後続 Issue にて以下を実施する。
  - JSON Schema（profile provider contract schema）を定義し、`schema_id`, `version`, `capability`, `credential_scope`, `routing_state` を必須化。
  - semantic validator を実装（意図／権限／安全条件を機械的に検証）。
  - positive / negative fixtures を最低 2 件ずつ作成。
  - `schemas/catalog.yaml` と `docs/dev/schema-governance.md` の catalog/schema governance（`supersedes` / `deprecation` / `backward compatibility`）を登録・整備。
  - consumer inventory を作成し、既存消費先（issue workflow / CLI / API 呼び出し経路）を列挙。
  - migration rule を後続 Issue の実装開始前に一択で選定し、
    - `atomic cutover`
    - `generated projection`
    - `dual-read`
    のいずれかだけを後続 Issue 契約に明記。**manual dual-write は禁止**。
- migration の具体選定は、後続 implementation Issue が契約として固定する。#1814 の未解決を理由に選定前の実装・検証・consumer migration を開始しない。

## Consequences
### 好影響
- profile/provider 仕様が設計文書で明確化され、後続実装時の解釈揺れが低減。
- 監査観点（安全、認証、状態遷移）が初期段階で固定されるため、再実装コストを抑制。

### トレードオフ
- 本 Issue で実装を伴わないため、短期的な動作変更はゼロ。
- migration 選定と fixture 作成は後続で着手する必要がある。

### 後続引き継ぎ
- 主要導線の実装・検証は Issue #1814 および当該 follow-up の issue で継続。

## Out of scope
以下は本 Issue の scope 外とする。
- matrix YAML の追加・修正
- schema v2 の定義/配布
- validator 実装（実コード）
- pytest / 自動テスト追加
- fixtures 実体ファイルの追加
- `schemas/catalog.yaml` と `docs/dev/schema-governance.md` の登録
- consumer migration 実装
- 外部実装・コード変更（本 Issue では実装なし）

## References（設計判断の根拠参照）
- Issue #1821
- Issue #1814
- ADR 0005
