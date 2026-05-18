# Result Surface

`gemini-cli-headless-delegation` の caller / orchestrator は、長文の `response_text` 全文をそのまま main thread に再注入するのではなく、まず **result surface** を見る。

## Goal

- full report の正本を artifact 側へ寄せる
- main thread には `summary` / `primary_artifact` / `next_action` だけを薄く返す
- `response_text` は detail が必要なときだけ読む long-form evidence として残す

## Result Surface Fields

`run_gemini_headless.py` の `delegation_result/v1` には `result_surface` を含める。

```json
{
  "result_surface": {
    "mode": "artifact-first",
    "summary": "1-2文の短い要約",
    "primary_artifact_type": "github_comment_url",
    "primary_artifact": "https://github.com/owner/repo/issues/123#issuecomment-1",
    "next_action": "Open the comment URL only if detailed evidence is needed."
  }
}
```

## Interpretation Rules

### 1. `primary_artifact_type == "github_comment_url"`

- `primary_artifact` は full report の正本 URL
- main thread は URL と summary だけを保持し、本文全文を取り込まない
- `next_action` は「必要時のみ comment を開く」動線を返す

### 2. `primary_artifact_type == "inline_response_text"`

- `primary_artifact` は `"response_text"` という field pointer
- wrapper 内で GitHub 投稿しなかった場合でも、caller はまず summary を見て判断する
- detail が必要なときだけ `response_text` を読む

### 3. `primary_artifact_type == "none"`

- fail-closed / empty response / 投稿失敗で usable artifact がない状態
- caller は `warnings` と `failure_reason` を見て停止または再試行する

## Caller Priority

1. `result_surface.summary`
2. `result_surface.primary_artifact`
3. `result_surface.next_action`
4. 必要なときだけ `response_text`

## Model Routing フィールド（result JSON の追加フィールド）

`run_gemini_headless.py` の `delegation_result_v1` には result surface 以外に以下の model routing フィールドが含まれる。

| フィールド | 型 | 説明 |
|---|---|---|
| `model_chain` | `list[str]` | 試行対象だった model のリスト（chain 全体）。常に存在。 |
| `model_downgrades` | `list[{from, to, reason}]` | 降格イベントのリスト。降格なし時は `[]`。常に存在。 |
| `reason_code` | `str` | fail-closed 理由コード（エラー時のみ設定）。値: `model_chain_exhausted` / `unknown_role` / `empty_chain` / `routing_config_invalid` |

## Non-Goals

- `response_text` 自体を削除すること
- caller 側の `proposal_only` integration (#1957) をここで再実装すること
- `local_asset_research` の read-only / fail-closed 境界を崩すこと
