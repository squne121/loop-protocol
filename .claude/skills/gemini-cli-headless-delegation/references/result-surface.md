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

## REPO_EVIDENCE_REF_V1 Output Examples

`local_asset_research` / `github_research` profile で file evidence を返す場合、以下の形式を使う。`response_text` / コメント / artifact の一部として embed される。

### Example 1: Verified File Evidence

```json
{
  "kind": "file",
  "evidence_ref": {
    "type": "REPO_EVIDENCE_REF_V1",
    "commit_sha": "abc123def456abc123def456abc123def456abc1",
    "path": "docs/dev/agent-skill-boundaries.md",
    "start_line": 42,
    "end_line": 67,
    "permalink": "https://github.com/squne121/loop-protocol/blob/abc123def456abc123def456abc123def456abc1/docs/dev/agent-skill-boundaries.md#L42-L67",
    "excerpt_sha256": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1",
    "anchor_text": "## Agent / Skill 責務境界",
    "verification_status": "verified",
    "verification_method": "sha256_hash_match",
    "verified_at": "2026-05-23T15:30:45Z"
  },
  "summary": "The file defines responsibility boundaries between SubAgent roles and Skill procedures."
}
```

### Example 2: Inconclusive File Evidence

```json
{
  "kind": "file",
  "evidence_ref": {
    "type": "REPO_EVIDENCE_REF_V1",
    "commit_sha": "def456abc123def456abc123def456abc123def4",
    "path": "src/systems/combat.ts",
    "start_line": 100,
    "end_line": 120,
    "permalink": "https://github.com/squne121/loop-protocol/blob/def456abc123def456abc123def456abc123def4/src/systems/combat.ts#L100-L120",
    "excerpt_sha256": "f0e1d2c3b4a5968778695a4b3c2d1e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5",
    "anchor_text": null,
    "verification_status": "inconclusive",
    "verification_method": "sha256_hash_mismatch",
    "verified_at": "2026-05-23T15:31:10Z"
  },
  "summary": "File excerpt could not be verified: SHA-256 mismatch suggests the line range may have drifted due to file updates. Human re-verification required."
}
```

**Caller behavior difference**:
- Example 1 (`verified`): Caller can confidently use this excerpt as authoritative code reference.
- Example 2 (`inconclusive`): Caller MUST escalate to human review or re-request file evidence with updated parameters. DO NOT use the provided line numbers as ground truth.

## Non-Goals

- `response_text` 自体を削除すること
- caller 側の `proposal_only` integration (#1957) をここで再実装すること
- `local_asset_research` の read-only / fail-closed 境界を崩すこと
